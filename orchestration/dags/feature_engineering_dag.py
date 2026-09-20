from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.tasks import compute_and_write_features

default_args = {"owner": "air-pollution-prediction", "retries": 2, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="feature_engineering_dag",
    description="Hourly feature computation for all active stations/pollutants",
    schedule="30 * * * *",  # offset after ingestion_dag's :10 run
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["features"],
) as dag:

    compute_features = PythonOperator(task_id="compute_and_write_features", python_callable=compute_and_write_features)
