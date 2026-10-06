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

    from common.regions import load_regions
    from db.session import get_session
    from orchestration.plugins.common.alerts import ping_healthcheck, send_telegram
    from orchestration.plugins.common.region_health import collect_region_health
    from orchestration.plugins.common.watchdog import all_issue_keys, evaluate_health

    now = datetime.now(timezone.utc)
    regions = load_regions()
    session = get_session()
    try:
        health = collect_region_health(session, now, regions)
    finally:
        session.close()

    ingestion = DagModel.get_dagmodel("ingestion_dag")
    issues = evaluate_health(now, regions=health, sensor_ingestion_enabled=bool(ingestion and not ingestion.is_paused))

    current_keys = {i.key for i in issues}
    for issue in issues:
        var = f"watchdog_alerted:{issue.key}"
        last = Variable.get(var, default_var=None)
        if last is None or now - datetime.fromisoformat(last) > REPEAT_AFTER:
            send_telegram(f"WATCHDOG: {issue.message}")
            Variable.set(var, now.isoformat())
    for key in all_issue_keys([r.id for r in regions]):
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
