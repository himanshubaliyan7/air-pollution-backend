"""Hourly: CPCB current-conditions AQI snapshot, from CPCB's own CAAQMS feed
(published on the IST hour, i.e. :30 UTC) with data.gov.in as the fallback.

Separate from ingestion_dag on purpose: a different provider and cadence, so
neither can stall the other.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

from orchestration.plugins.common.tasks import ingest_current_aqi

default_args = {"owner": "air-pollution-prediction", "on_failure_callback": notify_task_failure, "retries": 2, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="current_aqi_dag",
    description="Hourly CPCB current AQI snapshot (CPCB feed, data.gov.in fallback)",
    schedule="40 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["ingestion", "current-aqi"],
) as dag:
    PythonOperator(task_id="ingest_current_aqi", python_callable=ingest_current_aqi)
