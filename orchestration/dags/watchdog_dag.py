"""Hourly watchdog: checks the data invariants (orchestration/plugins/common/watchdog.py),
alerts once per issue on Telegram (again after 6h, and once when it clears), and pings a
dead-man's switch so silence itself becomes an alert (PC asleep, scheduler dead).
"""

from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

REPEAT_AFTER = timedelta(hours=6)

default_args = {
    "owner": "air-pollution-prediction",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "on_failure_callback": notify_task_failure,
}


def _run_watchdog(**_):
    from airflow.models import DagModel, Variable
    from sqlalchemy import func, select

    from common.config import get_settings
    from db.models import RawSensorReading, StationAqiSnapshot
    from db.session import get_session
    from orchestration.plugins.common.alerts import ping_healthcheck, send_telegram
    from orchestration.plugins.common.watchdog import evaluate_health

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        newest_snapshot = session.execute(select(func.max(StationAqiSnapshot.source_updated_at))).scalar_one_or_none()
        fresh = session.execute(
            select(func.count(func.distinct(StationAqiSnapshot.station_id))).where(
                StationAqiSnapshot.source_updated_at >= now - timedelta(hours=3)
            )
        ).scalar_one()
        newest_sensor = session.execute(select(func.max(RawSensorReading.observed_at))).scalar_one_or_none()
    finally:
        session.close()

    ingestion = DagModel.get_dagmodel("ingestion_dag")
    issues = evaluate_health(
        now,
        aqi_feed_enabled=bool(get_settings().data_gov_in_api_key),
        newest_snapshot=newest_snapshot,
        stations_with_fresh_snapshot=fresh,
        sensor_ingestion_enabled=bool(ingestion and not ingestion.is_paused),
        newest_sensor_reading=newest_sensor,
    )

    current_keys = {i.key for i in issues}
    for issue in issues:
        var = f"watchdog_alerted:{issue.key}"
        last = Variable.get(var, default_var=None)
        if last is None or now - datetime.fromisoformat(last) > REPEAT_AFTER:
            send_telegram(f"WATCHDOG: {issue.message}")
            Variable.set(var, now.isoformat())
    for key in ("aqi-feed-stale", "aqi-coverage-low", "sensor-data-stale"):
        var = f"watchdog_alerted:{key}"
        if key not in current_keys and Variable.get(var, default_var=None) is not None:
            send_telegram(f"WATCHDOG: resolved - {key}")
            Variable.delete(var)

    ping_healthcheck(ok=not issues)
    return [i.key for i in issues]


with DAG(
    dag_id="watchdog_dag",
    description="Hourly data-invariant checks with Telegram alerts and a dead-man's-switch ping",
    schedule="55 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["monitoring"],
) as dag:
    PythonOperator(task_id="check_data_invariants", python_callable=_run_watchdog)
