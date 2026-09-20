"""Hourly: pull OpenAQ sensor readings + ERA5/ERA5T weather, reconcile
ERA5T -> final ERA5 for the ~5-6 day-old window, pull the Open-Meteo
near-term forecast (fills the real gap ERA5 leaves for the last ~5 days -
see ingestion/weather/open_meteo_client.py), and sanity-check freshness.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.tasks import (
    ingest_era5_recent,
    ingest_open_meteo_forecast,
    ingest_sensor_readings,
    ingestion_data_quality_check,
    reconcile_era5_final,
)

default_args = {"owner": "air-pollution-prediction", "retries": 2, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="ingestion_dag",
    description="Hourly OpenAQ + ERA5 + Open-Meteo ingestion",
    schedule="10 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["ingestion"],
) as dag:

    fetch_openaq = PythonOperator(task_id="fetch_openaq_readings", python_callable=ingest_sensor_readings)

    fetch_era5_latest = PythonOperator(task_id="fetch_era5_latest", python_callable=ingest_era5_recent)

    reconcile_era5 = PythonOperator(task_id="reconcile_era5_final", python_callable=reconcile_era5_final)

    fetch_open_meteo = PythonOperator(task_id="fetch_open_meteo_forecast", python_callable=ingest_open_meteo_forecast)

    def _quality_check(ti, **_):
        sensor_rows = ti.xcom_pull(task_ids="fetch_openaq_readings") or 0
        weather_rows = ti.xcom_pull(task_ids="fetch_era5_latest") or 0
        weather_rows += ti.xcom_pull(task_ids="fetch_open_meteo_forecast") or 0
        ingestion_data_quality_check(sensor_rows, weather_rows)

    quality_check = PythonOperator(task_id="ingestion_data_quality_check", python_callable=_quality_check)

    # Open-Meteo is independent of the ERA5 archive chain - an ERA5/CDS failure
    # must not stop the near-term weather forecast (the inference-time input).
    fetch_openaq >> fetch_era5_latest >> reconcile_era5 >> quality_check
    fetch_openaq >> fetch_open_meteo >> quality_check
