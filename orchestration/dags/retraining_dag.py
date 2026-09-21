from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

from orchestration.plugins.common.tasks import retrain_all

default_args = {"owner": "air-pollution-prediction", "on_failure_callback": notify_task_failure, "retries": 1, "retry_delay": timedelta(minutes=30)}

with DAG(
    dag_id="retraining_dag",
    description="Weekly: retrain quantile + classifier models, promote only if they beat the incumbent",
    schedule="@weekly",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["training"],
) as dag:

    # train_station_pollutant_horizon (called per station/pollutant/horizon
    # inside retrain_all) already does: fit quantiles + classifier -> evaluate
    # on a held-out recent window -> register_model_run -> promote_if_better.
    # See models/train.py for why this is one task rather than the plan's
    # more granular train/evaluate/register/promote task split: those four
    # steps share in-memory state (fitted boosters, holdout predictions)
    # that isn't worth round-tripping through Airflow XCom/artifacts.
    retrain = PythonOperator(task_id="retrain_evaluate_register_promote", python_callable=retrain_all)
