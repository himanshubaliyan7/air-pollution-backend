"""The daily digest: one email per confirmed subscriber per day (owner decision
2026-09-25), replacing an email per forecast threshold crossing.

It is sent every day, whatever the outlook. Every station/pollutant gets an
explicit verdict for tomorrow and the two days after - go and no-data
included - because an email only on bad days would make silence read as "go",
which the product must never do. Verdicts come from models.outlook, the same
code as the website, so the two cannot disagree.

Idempotent: digest_log holds one row per (subscriber, digest day); a day
already marked sent is never emailed again, a failed one is retried.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from alerting.mailer import build_message, send_message
from alerting.tokens import make_unsubscribe_token, one_click_unsubscribe_url, unsubscribe_page_link
from common.config import REPO_ROOT, get_settings
from common.constants import AlertStatus, Pollutant
from db.models import AlertSubscription, DigestLog, Station
from models.outlook import Outlook, build_outlook, station_timezone

logger = logging.getLogger(__name__)

DAYS_SHOWN = 3  # tomorrow + the two days after
_VERDICT_ORDER = ["no-go", "caution", "no-data", "go"]  # most to least urgent, for the subject line
_LABEL = {"no-go": "No-go", "caution": "Caution", "no-data": "No data", "go": "Go"}

_env = Environment(
    loader=FileSystemLoader(str(REPO_ROOT / "alerting" / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)


@dataclass(frozen=True)
class DigestRow:
    station_name: str
    pollutant: str  # display form, e.g. "PM2.5"
    verdicts: list[str]  # one per day shown
    data_as_of: str | None  # local time of the newest reading behind the forecast


def _pollutant_label(p: str) -> str:
    return {"pm25": "PM2.5", "no2": "NO2"}.get(p, p.upper())


def _rows_for(
    session: Session, sub: AlertSubscription, days: list[date], tz: ZoneInfo, now: datetime, cache: dict
) -> list[DigestRow]:
    rows = []
    for station_id in sub.station_ids:
        station = session.get(Station, station_id)
        for p in sub.pollutants:
            if station is None or not station.is_active:
                # Retired station: say so rather than silently dropping it.
                rows.append(DigestRow(station_id if station is None else station.name, _pollutant_label(p),
                                      ["no-data"] * len(days), None))
                continue
            key = (station_id, p)
            if key not in cache:
                cache[key] = build_outlook(session, station, Pollutant(p), days_ahead=5, now=now)
            outlook: Outlook = cache[key]
            as_of = outlook.forecast_made_at.astimezone(tz).strftime("%a %d %b %H:%M") if outlook.forecast_made_at else None
            rows.append(DigestRow(station.name, _pollutant_label(p), [outlook.verdict_for(d) for d in days], as_of))
    return rows


def _subject(tomorrow: date, rows: list[DigestRow]) -> str:
    firsts = [r.verdicts[0] for r in rows]
    worst = min(firsts, key=_VERDICT_ORDER.index)
    day = tomorrow.strftime("%a %d %b")
    if worst == "go":
        return f"Outdoor practice outlook for {day}: go at all your stations"
    n = firsts.count(worst)
    return f"Outdoor practice outlook for {day}: {_LABEL[worst].lower()} for {n} of {len(rows)}"


def _already_sent(session: Session, subscriber_id, digest_date: date) -> DigestLog | None:
    return session.execute(
        select(DigestLog).where(DigestLog.subscriber_id == subscriber_id, DigestLog.digest_date == digest_date)
    ).scalar_one_or_none()


def send_daily_digests(session: Session, now: datetime) -> dict[str, int]:
    settings = get_settings()
    subscribers = session.execute(
        select(AlertSubscription).where(AlertSubscription.is_confirmed.is_(True), AlertSubscription.is_active.is_(True))
    ).scalars().all()
    cache: dict = {}
    result = {"sent": 0, "failed": 0, "skipped": 0, "not_invited": 0}

    for sub in subscribers:
        if not settings.may_subscribe(sub.email):  # demo mode, e.g. an address removed from the invite list
            result["not_invited"] += 1
            continue
        first = session.get(Station, sub.station_ids[0]) if sub.station_ids else None
        tz = ZoneInfo(station_timezone(first) if first else "UTC")
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        log = _already_sent(session, sub.subscriber_id, tomorrow)
        if log is not None and log.status == AlertStatus.SENT:
            result["skipped"] += 1
            continue

        days = [tomorrow + timedelta(days=i) for i in range(DAYS_SHOWN)]
        rows = _rows_for(session, sub, days, tz, now, cache)
        token = make_unsubscribe_token(sub.subscriber_id, settings)
        base = (settings.frontend_base_url or settings.dashboard_url).rstrip("/")
        context = dict(
            day_labels=[d.strftime("%a %d %b") for d in days],
            rows=rows,
            labels=_LABEL,
            site_url=base,
            manage_request_url=f"{base}/subscribe",
            unsubscribe_url=unsubscribe_page_link(token, settings),
        )
        status, error = AlertStatus.SENT, None
        try:
            msg = build_message(
                to=sub.email,
                subject=_subject(tomorrow, rows),
                text=_env.get_template("digest.txt").render(**context),
                html_body=_env.get_template("digest.html.j2").render(**context),
                # RFC 8058 one-click unsubscribe: the mail provider POSTs to this URL.
                extra_headers={
                    "List-Unsubscribe": f"<{one_click_unsubscribe_url(token, settings)}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            )
            if not send_message(msg):  # SMTP not configured: nothing was sent
                raise RuntimeError("SMTP_HOST not configured")
        except Exception as exc:  # noqa: BLE001 - recorded per subscriber, one failure must not stop the rest
            status, error = AlertStatus.FAILED, str(exc)[:1000]
            logger.exception("Digest to subscriber %s failed", sub.subscriber_id)

        if log is None:
            session.add(DigestLog(id=uuid.uuid4(), subscriber_id=sub.subscriber_id, digest_date=tomorrow,
                                  sent_at=now, status=status, error_detail=error))
        else:  # retrying an earlier failed attempt for the same day
            log.status, log.sent_at, log.error_detail = status, now, error
        session.commit()
        result["sent" if status == AlertStatus.SENT else "failed"] += 1

    logger.info("Daily digest: %s", result)
    return result
