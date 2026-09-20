"""Daily: deactivate stations OpenAQ reports as dark for 30+ days (and
reactivate any that resume), so hourly ingestion stops spending API quota on
them. See tasks.refresh_station_activity.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.tasks import refresh_station_activity

default_args = {"owner": "air-pollution-prediction", "retries": 2, "retry_delay": timedelta(minutes=10)}

with DAG(
    dag_id="station_maintenance_dag",
    description="Daily station activity refresh from OpenAQ last-reported times",
    schedule="30 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["maintenance"],
) as dag:
    PythonOperator(task_id="refresh_station_activity", python_callable=refresh_station_activity)
