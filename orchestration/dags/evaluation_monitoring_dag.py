from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from orchestration.plugins.common.alerts import notify_task_failure

from orchestration.plugins.common.tasks import daily_verdict_drift, evaluate_recent_forecasts

default_args = {"owner": "air-pollution-prediction", "on_failure_callback": notify_task_failure, "retries": 1, "retry_delay": timedelta(minutes=10)}

MIN_RECALL = 0.4  # below this, maintainers (not schools) get paged - see drift_check


def _evaluate_and_check_drift(**_):
    from sqlalchemy import select

    from common.config import forecast_target, utc_now
    from common.constants import ForecastTarget
    from db.models import ExceedanceEvaluation
    from db.session import get_session
    from models.evaluation import recall_drift_detected

    written = evaluate_recent_forecasts()

    session = get_session()
    try:
        if forecast_target() is ForecastTarget.DAILY_MEAN:
            # The rule below reads one evaluation's recall, which for a
            # one-day window is 0 or 1; the daily family has its own.
            problem = daily_verdict_drift(session, utc_now())
            if problem:
                raise RuntimeError(problem)
            return written
        recent = session.execute(
            select(ExceedanceEvaluation.recall)
            .where(ExceedanceEvaluation.recall.is_not(None))
            .order_by(ExceedanceEvaluation.computed_at.desc())
            .limit(20)
        ).scalars().all()
    finally:
        session.close()

    if recall_drift_detected(recent, MIN_RECALL):
        low_recall = [r for r in recent if r < MIN_RECALL]
        # Intentionally just a loud log/task-failure (surfaces in Airflow's
        # own alerting) rather than the school-facing alerting/ path - this
        # is a maintainer-facing model-drift signal, not a health alert.
        raise RuntimeError(
            f"Model drift check failed: {len(low_recall)}/{len(recent)} recent evaluations "
            f"below recall floor {MIN_RECALL} - retraining_dag may need to run sooner than weekly"
        )
    return written


with DAG(
    dag_id="evaluation_monitoring_dag",
    description="Daily: score recent forecasts against realized readings, watch for drift",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["evaluation"],
) as dag:

    evaluate_and_check_drift = PythonOperator(
        task_id="evaluate_and_check_drift", python_callable=_evaluate_and_check_drift
    )
