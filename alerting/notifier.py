"""SMTP email sender for threshold-crossing alerts.

Idempotency: every send attempt (success or failure) writes an alert_log
row; a send is skipped if a 'sent' row already exists for the exact same
(subscriber, station, pollutant, forecast) tuple - this is what makes a DAG
task retry safe (won't double-email a school) and is enforced by
alert_log's uq_alert_log_idempotency unique constraint as a backstop even if
the pre-check below racing.
"""

import logging
import uuid
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from alerting.mailer import build_message, send_message
from alerting.tokens import make_unsubscribe_token, one_click_unsubscribe_url, unsubscribe_page_link
from common.config import REPO_ROOT, get_settings, to_ist
from db.models import AlertLog, AlertSubscription

logger = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(str(REPO_ROOT / "alerting" / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)


def _already_sent(session: Session, subscriber_id, station_id, pollutant, model_id, forecast_made_at, target_time) -> bool:
    stmt = select(AlertLog).where(
        AlertLog.subscriber_id == subscriber_id,
        AlertLog.station_id == station_id,
        AlertLog.pollutant == pollutant,
        AlertLog.forecast_model_id == model_id,
        AlertLog.forecast_made_at == forecast_made_at,
        AlertLog.target_time == target_time,
        AlertLog.status == "sent",
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def send_exceedance_alert(
    session: Session,
    subscriber: AlertSubscription,
    *,
    station_id: str,
    station_name: str,
    pollutant: str,
    aqi_category: str,
    forecast_value: float,
    model_id: uuid.UUID,
    forecast_made_at: datetime,
    target_time: datetime,
    dashboard_url: str = "http://localhost:8501",
) -> bool:
    if _already_sent(session, subscriber.subscriber_id, station_id, pollutant, model_id, forecast_made_at, target_time):
        logger.info("Alert already sent for %s/%s/%s - skipping", subscriber.email, station_id, target_time)
        return False

    settings = get_settings()
    unsubscribe_token = make_unsubscribe_token(subscriber.subscriber_id, settings)
    context = dict(
        station_name=station_name,
        pollutant=pollutant.upper(),
        target_date=to_ist(target_time).date().isoformat(),
        forecast_value=round(forecast_value, 1),
        aqi_category=aqi_category.replace("_", " ").title(),
        dashboard_url=dashboard_url,
        unsubscribe_url=unsubscribe_page_link(unsubscribe_token, settings),
    )
    html = _env.get_template("exceedance_alert.html.j2").render(**context)
    text = _env.get_template("exceedance_alert.txt").render(**context)

    status = "sent"
    error_detail = None
    try:
        msg = build_message(
            to=subscriber.email,
            subject=f"Air quality alert: {station_name} ({pollutant.upper()})",
            text=text,
            html_body=html,
            # RFC 8058 one-click unsubscribe: the mail provider POSTs to this URL.
            extra_headers={
                "List-Unsubscribe": f"<{one_click_unsubscribe_url(unsubscribe_token, settings)}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
        send_message(msg)  # logs and skips when SMTP_HOST is unset (dev)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
        status = "failed"
        error_detail = str(exc)
        logger.exception("Failed to send alert to %s", subscriber.email)

    session.add(
        AlertLog(
            id=uuid.uuid4(),
            subscriber_id=subscriber.subscriber_id,
            station_id=station_id,
            pollutant=pollutant,
            forecast_model_id=model_id,
            forecast_made_at=forecast_made_at,
            target_time=target_time,
            sent_at=datetime.now(timezone.utc),
            status=status,
            error_detail=error_detail,
        )
    )
    session.commit()
    return status == "sent"
