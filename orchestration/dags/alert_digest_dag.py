"""Daily 18:00 IST: email every confirmed subscriber tomorrow's outdoor
practice outlook (alerting/digest.py), early enough in the evening for a
school to plan the next day. It uses whatever forecast run is current (inputs
up to 24 h old); a station without one is listed as no-data.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

from orchestration.plugins.common.tasks import send_daily_digest

default_args = {"owner": "air-pollution-prediction", "on_failure_callback": notify_task_failure, "retries": 2, "retry_delay": timedelta(minutes=10)}

with DAG(
    dag_id="alert_digest_dag",
    description="Daily 18:00 IST digest of tomorrow's outlook to confirmed subscribers",
    schedule="30 12 * * *",  # 12:30 UTC = 18:00 IST
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["alerts"],
) as dag:
    PythonOperator(task_id="send_daily_digest", python_callable=send_daily_digest)
