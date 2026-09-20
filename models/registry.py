"""Model artifact storage + the model_runs registry table.

No separate model-registry service (MLflow etc.) - model_runs rows are the
registry; is_active marks which row models/predict.py and forecast_dag load
for a given (station, pollutant, horizon, model_type[, quantile]). Flipped
only by retraining_dag's promote_if_better step, never by hand.
"""

import logging
import uuid
from datetime import datetime

import lightgbm as lgb
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from common.config import get_settings
from common.constants import ModelType, Pollutant
from db.models import ModelRun

logger = logging.getLogger(__name__)


def artifact_path(station_id: str, pollutant: Pollutant, horizon_hours: int, model_type: ModelType, trained_at: datetime, quantile: float | None = None) -> str:
    settings = get_settings()
    base = settings.model_artifacts_dir / station_id.replace(":", "_") / pollutant.value / str(horizon_hours) / model_type.value
    base.mkdir(parents=True, exist_ok=True)
    suffix = f"_q{quantile}" if quantile is not None else ""
    filename = f"{trained_at.strftime('%Y%m%dT%H%M%S')}{suffix}.txt"
    return str(base / filename)


def save_booster(booster: lgb.Booster, path: str) -> None:
    booster.save_model(path)


def load_booster(path: str) -> lgb.Booster:
    return lgb.Booster(model_file=path)


def register_model_run(
    session: Session,
    *,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    model_type: ModelType,
    feature_set_version: str,
    artifact_path: str,
    trained_at: datetime,
    training_window_start: datetime,
    training_window_end: datetime,
    metrics: dict,
    hyperparams: dict,
    quantile: float | None = None,
    is_active: bool = False,
) -> uuid.UUID:
    model_id = uuid.uuid4()
    session.add(
        ModelRun(
            model_id=model_id,
            station_id=station_id,
            pollutant=pollutant,
            horizon_hours=horizon_hours,
            model_type=model_type,
            quantile=quantile,
            feature_set_version=feature_set_version,
            artifact_path=artifact_path,
            trained_at=trained_at,
            training_window_start=training_window_start,
            training_window_end=training_window_end,
            metrics=metrics,
            hyperparams=hyperparams,
            is_active=is_active,
        )
    )
    session.commit()
    return model_id


def activate_model(session: Session, model_id: uuid.UUID) -> None:
    row = session.execute(select(ModelRun).where(ModelRun.model_id == model_id)).scalar_one()
    session.execute(
        update(ModelRun)
        .where(
            ModelRun.station_id == row.station_id,
            ModelRun.pollutant == row.pollutant,
            ModelRun.horizon_hours == row.horizon_hours,
            ModelRun.model_type == row.model_type,
            ModelRun.quantile.is_(row.quantile) if row.quantile is None else ModelRun.quantile == row.quantile,
        )
        .values(is_active=False)
    )
    row.is_active = True
    session.commit()


def promote_if_better(
    session: Session,
    *,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    model_type: ModelType,
    candidate_model_ids: list[uuid.UUID],
    candidate_f1: float,
    min_improvement: float = 0.0,
) -> bool:
    """Activates candidate_model_ids (deactivating whatever was previously
    active for this key) only if candidate_f1 beats the currently active
    set's F1 by at least min_improvement, or nothing is active yet. Applied
    as one unit across candidate_model_ids so a quantile regressor's three
    per-quantile artifacts (0.1/0.5/0.9) are promoted or held back together -
    see models/train.py, where the median (0.5) row carries the merged
    exceedance-classification metrics used for this comparison.

    Guards against silent regression: a newly retrained model never
    replaces a better-performing incumbent just because it's newer.
    """
    currently_active = get_active_models(session, station_id, pollutant, horizon_hours, model_type)
    active_f1 = None
    for row in currently_active:
        if row.metrics and "f1" in row.metrics:
            active_f1 = row.metrics["f1"]
            break

    should_promote = active_f1 is None or candidate_f1 >= active_f1 + min_improvement
    if not should_promote:
        logger.info(
            "Not promoting %s/%s/%dh/%s: candidate f1=%.3f does not beat active f1=%.3f",
            station_id, pollutant.value, horizon_hours, model_type.value, candidate_f1, active_f1,
        )
        return False

    for row in currently_active:
        row.is_active = False
    for model_id in candidate_model_ids:
        row = session.execute(select(ModelRun).where(ModelRun.model_id == model_id)).scalar_one()
        row.is_active = True
    session.commit()
    logger.info(
        "Promoted %s/%s/%dh/%s: new f1=%.3f (previous=%s)",
        station_id, pollutant.value, horizon_hours, model_type.value, candidate_f1, active_f1,
    )
    return True


def get_active_models(
    session: Session, station_id: str, pollutant: Pollutant, horizon_hours: int, model_type: ModelType
) -> list[ModelRun]:
    stmt = select(ModelRun).where(
        ModelRun.station_id == station_id,
        ModelRun.pollutant == pollutant,
        ModelRun.horizon_hours == horizon_hours,
        ModelRun.model_type == model_type,
        ModelRun.is_active.is_(True),
    )
    return list(session.execute(stmt).scalars().all())
