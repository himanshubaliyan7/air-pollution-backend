from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.tasks import generate_forecasts, trigger_alerts_for_crossings

default_args = {"owner": "air-pollution-prediction", "retries": 2, "retry_delay": timedelta(minutes=5)}

with DAG(
    dag_id="forecast_dag",
    description="Hourly: generate forecasts + exceedance signal, alert on newly-crossed thresholds",
    schedule="45 * * * *",  # offset after feature_engineering_dag's :30 run
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["forecast"],
) as dag:

    def _generate(ti, **_):
        result = generate_forecasts()
        ti.xcom_push(key="new_crossings", value=result["new_crossings"])
        return result["forecasts_written"]

    generate = PythonOperator(task_id="generate_point_and_quantile_forecasts", python_callable=_generate)

    def _trigger_alerts(ti, **_):
        crossings = ti.xcom_pull(task_ids="generate_point_and_quantile_forecasts", key="new_crossings") or []
        return trigger_alerts_for_crossings(crossings)

    trigger_alerts = PythonOperator(task_id="trigger_alerts", python_callable=_trigger_alerts)

    generate >> trigger_alerts
