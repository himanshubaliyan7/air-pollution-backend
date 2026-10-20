"""Operator alerts: Telegram messages, and a dead-man's-switch ping.

Silent no-ops until TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (and, for the ping,
HEALTHCHECKS_PING_URL) are configured, so the pipeline never fails because alerting is
not set up. These are operator alerts (something broke), not the school-facing alerts
in alerting/.
"""

import logging
import re

import requests

from common.config import get_settings

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = [
    re.compile(r"(api[-_]?key=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s,'\"]+", re.IGNORECASE),
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]+", re.IGNORECASE),
]


def redact(text: str) -> str:
    """Exception messages from `requests` embed the full URL, including ?api-key=...;
    never let a key reach a chat."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "<redacted>" if m.lastindex else "<redacted>", text)
    return text


def send_telegram(text: str) -> bool:
    """True if a message was sent. False (never an exception) when unconfigured or failing:
    the alert path must not break the job it is reporting on."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.info("Telegram not configured; alert not sent: %s", redact(text)[:200])
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": redact(text)[:3500]},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Telegram rejected the alert (HTTP %s)", resp.status_code)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Could not reach Telegram: %s", redact(str(exc)))
        return False


def ping_healthcheck(ok: bool) -> bool:
    """Dead-man's switch (e.g. healthchecks.io): if these pings STOP arriving, the
    external service alerts us - which is how a sleeping PC or a dead scheduler is noticed."""
    url = get_settings().healthchecks_ping_url
    if not url:
        return False
    try:
        requests.get(url if ok else url.rstrip("/") + "/fail", timeout=15)
        return True
    except requests.RequestException as exc:
        logger.warning("Healthcheck ping failed: %s", redact(str(exc)))
        return False


def history_line(previous_states: list[str], last_success) -> str:
    """'3rd failed run in a row; last success ...': a one-off and a job stuck for days
    need different handling, and the alert alone did not say which. previous_states are
    the DAG's earlier runs, newest first."""
    streak = 1
    for state in previous_states:
        if state != "failed":
            break
        streak += 1
    first = "first failure after a good run" if streak == 1 else f"failed run {streak} in a row"
    when = f"last success {last_success:%Y-%m-%d %H:%M} UTC" if last_success else "no successful run on record"
    return f"{first}; {when}"


def _run_history(dag_id: str, run_id: str | None) -> str:
    from airflow.models import DagRun
    from airflow.utils.session import create_session

    with create_session() as session:
        runs = (
            session.query(DagRun.run_id, DagRun.state)
            .filter(DagRun.dag_id == dag_id)
            .order_by(DagRun.execution_date.desc())
            .limit(30)
            .all()
        )
        success = (
            session.query(DagRun.end_date)
            .filter(DagRun.dag_id == dag_id, DagRun.state == "success")
            .order_by(DagRun.execution_date.desc())
            .first()
        )
    return history_line([state for rid, state in runs if rid != run_id], success[0] if success else None)


def notify_task_failure(context: dict) -> None:
    """Airflow on_failure_callback: one Telegram message per failed task attempt."""
    try:
        ti = context.get("task_instance")
        exc = context.get("exception")
        dag_id = getattr(ti, "dag_id", "?")
        try:
            history = "\n" + _run_history(dag_id, getattr(ti, "run_id", None))
        except Exception:  # noqa: BLE001 - outside Airflow, or its database is down
            history = ""
        send_telegram(
            f"FAILED: {dag_id}.{getattr(ti, 'task_id', '?')} "
            f"(attempt {getattr(ti, 'try_number', '?')}){history}\n"
            f"{type(exc).__name__ if exc else ''}: {str(exc)[:1500] if exc else ''}"
        )
    except Exception:  # noqa: BLE001 - a broken alert must never mask the original failure
        logger.exception("notify_task_failure itself failed")
