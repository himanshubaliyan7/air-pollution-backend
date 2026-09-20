"""SMTP email sender for threshold-crossing alerts.

Idempotency: every send attempt (success or failure) writes an alert_log
row; a send is skipped if a 'sent' row already exists for the exact same
(subscriber, station, pollutant, forecast) tuple - this is what makes a DAG
task retry safe (won't double-email a school) and is enforced by
alert_log's uq_alert_log_idempotency unique constraint as a backstop even if
the pre-check below racing.
"""

import logging
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    template = _env.get_template("exceedance_alert.html.j2")
    html = template.render(
        station_name=station_name,
        pollutant=pollutant.upper(),
        target_date=to_ist(target_time).date().isoformat(),
        forecast_value=round(forecast_value, 1),
        aqi_category=aqi_category.replace("_", " ").title(),
        dashboard_url=dashboard_url,
    )

    status = "sent"
    error_detail = None
    try:
        if settings.smtp_host:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Air quality alert: {station_name} ({pollutant.upper()})"
            msg["From"] = settings.alert_from_address
            msg["To"] = subscriber.email
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                if settings.smtp_user:
                    smtp.starttls()
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.alert_from_address, [subscriber.email], msg.as_string())
        else:
            logger.warning("SMTP_HOST not configured - logging alert instead of sending: %s -> %s", station_name, subscriber.email)
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
