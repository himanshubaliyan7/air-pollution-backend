from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

from orchestration.plugins.common.tasks import generate_forecasts

default_args = {"owner": "air-pollution-prediction", "on_failure_callback": notify_task_failure, "retries": 2, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="forecast_dag",
    description="Hourly: generate forecasts + exceedance signal (alerts go out in alert_digest_dag)",
    schedule="45 * * * *",  # offset after feature_engineering_dag's :30 run
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["forecast"],
) as dag:

    PythonOperator(
        task_id="generate_point_and_quantile_forecasts",
        python_callable=lambda **_: generate_forecasts()["forecasts_written"],
    )
