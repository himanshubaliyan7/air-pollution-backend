"""Per station x pollutant x horizon training entrypoint.

Direct multi-horizon strategy: a separate model per horizon (not recursive
single-step forecasting), matching the plan's requirement. For each horizon
this fits the quantile regressors (primary v1 exceedance signal, via
models/exceedance.probability_from_quantiles) and, for comparison, a direct
binary exceedance classifier - both are registered so retraining_dag's
promote_if_better can pick whichever is doing better on held-out
precision/recall/F1 without any downstream code change.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import get_settings, load_yaml_config
from common.constants import ModelType, Pollutant
from db.models import ModelRun, RawSensorReading
from features.feature_store import get_feature_set_version, read_features
from models import evaluation, exceedance, registry
from models.lightgbm_pipeline import fit_classifier, fit_point_model, fit_quantile_model, predict, prepare_X

logger = logging.getLogger(__name__)


def _f1(metrics: dict) -> float:
    """F1 for logging and promotion. It is None when the holdout had no real and
    no predicted exceedances (common in the monsoon); promotion then compares it
    as 0.0, as it always did before undefined metrics became None (2026-09-25)."""
    return metrics["f1"] if metrics["f1"] is not None else 0.0


def load_model_config() -> dict:
    return load_yaml_config(get_settings().model_config_path)


def _fetch_target_series(session: Session, station_id: str, pollutant: Pollutant, start: datetime, end: datetime) -> pd.Series:
    stmt = select(RawSensorReading.observed_at, RawSensorReading.value).where(
        RawSensorReading.station_id == station_id,
        RawSensorReading.pollutant == pollutant,
        RawSensorReading.observed_at >= start,
        RawSensorReading.observed_at <= end,
    )
    rows = session.execute(stmt).all()
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([r.observed_at for r in rows], tz="UTC")
    return pd.Series([r.value for r in rows], index=idx).groupby(level=0).mean()


def build_training_matrix(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    window_start: datetime,
    window_end: datetime,
) -> tuple[pd.DataFrame, pd.Series]:
    """X = feature vectors already materialized in `features` (batch feature
    engineering), y = the realized pollutant value at feature_time + horizon.
    Rows with no realized label yet (too recent) or incomplete features are
    dropped - this is a supervised-learning matrix, not a serving path, so
    dropping NaNs here is correct (unlike build_feature_frame, which must
    tolerate them for cold-start stations)."""
    feature_frame = read_features(session, station_id, pollutant, window_start, window_end)
    if feature_frame.empty:
        return feature_frame, pd.Series(dtype="float64")

    target_start = window_start + timedelta(hours=horizon_hours)
    target_end = window_end + timedelta(hours=horizon_hours)
    target_series = _fetch_target_series(session, station_id, pollutant, target_start, target_end)

    label_index = feature_frame.index + pd.Timedelta(hours=horizon_hours)
    labels = target_series.reindex(label_index)
    labels.index = feature_frame.index

    combined = feature_frame.copy()
    combined["__label__"] = labels.values
    combined = combined.dropna()

    y = combined.pop("__label__")
    X = prepare_X(combined)
    return X, y


def train_station_pollutant_horizon(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    window_start: datetime,
    window_end: datetime,
    holdout_days: int,
) -> list:
    """Returns the list of newly-registered (not yet activated) model_run ids."""
    config = load_model_config()
    quantiles = config["quantiles"]
    lgbm_cfg = config["lightgbm"]
    min_rows = config["training"]["min_training_rows"]

    holdout_start = window_end - timedelta(days=holdout_days)

    X, y = build_training_matrix(session, station_id, pollutant, horizon_hours, window_start, window_end)
    if len(X) < min_rows:
        logger.warning(
            "Not enough training rows for %s/%s/%dh (%d < %d) - skipping",
            station_id, pollutant.value, horizon_hours, len(X), min_rows,
        )
        return []

    train_mask = X.index < holdout_start
    X_train, y_train = X[train_mask], y[train_mask]
    X_hold, y_hold = X[~train_mask], y[~train_mask]
    if len(X_hold) == 0 or len(X_train) == 0:
        logger.warning("Empty train or holdout split for %s/%s/%dh - skipping", station_id, pollutant.value, horizon_hours)
        return []

    trained_at = datetime.now(timezone.utc)
    feature_set_version = get_feature_set_version()
    thresholds = exceedance.load_thresholds()
    threshold_conc = exceedance.get_health_threshold_concentration(pollutant, thresholds)

    registered_ids = []
    quantile_holdout_preds: dict[float, np.ndarray] = {}
    quantile_model_ids: dict[float, uuid.UUID] = {}

    for q in quantiles:
        booster = fit_quantile_model(X_train, y_train, q, lgbm_cfg["quantile"])
        preds = predict(booster, X_hold)
        quantile_holdout_preds[q] = preds

        metrics = {"pinball_loss": evaluation.pinball_loss(y_hold.values, preds, q)}
        path = registry.artifact_path(station_id, pollutant, horizon_hours, ModelType.QUANTILE_REGRESSOR, trained_at, quantile=q)
        registry.save_booster(booster, path)
        model_id = registry.register_model_run(
            session,
            station_id=station_id,
            pollutant=pollutant,
            horizon_hours=horizon_hours,
            model_type=ModelType.QUANTILE_REGRESSOR,
            feature_set_version=feature_set_version,
            artifact_path=path,
            trained_at=trained_at,
            training_window_start=window_start,
            training_window_end=holdout_start,
            metrics=metrics,
            hyperparams=lgbm_cfg["quantile"],
            quantile=q,
        )
        registered_ids.append(model_id)
        quantile_model_ids[q] = model_id

    # Quantile-derived exceedance probability on the holdout set, for the
    # primary v1 signal's metrics.
    quantile_probs = np.array(
        [
            exceedance.probability_from_quantiles(
                {q: quantile_holdout_preds[q][i] for q in quantiles}, threshold_conc
            )
            for i in range(len(X_hold))
        ]
    )
    y_hold_binary = (y_hold.values > threshold_conc).astype(int)
    quantile_exceedance_metrics = evaluation.exceedance_classification_metrics(
        y_hold_binary, (quantile_probs >= thresholds["exceedance_probability_decision_threshold"]).astype(int)
    )
    quantile_exceedance_metrics.update(evaluation.regression_metrics(y_hold.values, quantile_holdout_preds[0.5]))

    # Recorded onto the median (0.5) quantile model's row - that's the
    # "quantile-derived" signal's overall metrics, for the Model Health
    # dashboard and for promote_if_better's comparison against the
    # classifier's metrics below.
    median_row = session.get(ModelRun, quantile_model_ids[0.5])
    median_row.metrics = {**median_row.metrics, **quantile_exceedance_metrics}
    session.commit()

    # Comparison direct classifier.
    y_train_binary = (y_train.values > threshold_conc).astype(int)
    clf_booster = fit_classifier(X_train, y_train_binary, lgbm_cfg["classifier"])
    clf_probs = predict(clf_booster, X_hold)
    clf_metrics = evaluation.exceedance_classification_metrics(
        y_hold_binary, (clf_probs >= thresholds["exceedance_probability_decision_threshold"]).astype(int)
    )
    clf_path = registry.artifact_path(station_id, pollutant, horizon_hours, ModelType.CLASSIFIER, trained_at)
    registry.save_booster(clf_booster, clf_path)
    clf_model_id = registry.register_model_run(
        session,
        station_id=station_id,
        pollutant=pollutant,
        horizon_hours=horizon_hours,
        model_type=ModelType.CLASSIFIER,
        feature_set_version=feature_set_version,
        artifact_path=clf_path,
        trained_at=trained_at,
        training_window_start=window_start,
        training_window_end=holdout_start,
        metrics=clf_metrics,
        hyperparams=lgbm_cfg["classifier"],
    )
    registered_ids.append(clf_model_id)

    logger.info(
        "Trained %s/%s/%dh: quantile-derived f1=%.3f, classifier f1=%.3f (holdout n=%d)",
        station_id, pollutant.value, horizon_hours,
        _f1(quantile_exceedance_metrics), _f1(clf_metrics), len(X_hold),
    )

    registry.promote_if_better(
        session,
        station_id=station_id,
        pollutant=pollutant,
        horizon_hours=horizon_hours,
        model_type=ModelType.QUANTILE_REGRESSOR,
        candidate_model_ids=[quantile_model_ids[q] for q in quantiles],
        candidate_f1=_f1(quantile_exceedance_metrics),
    )
    registry.promote_if_better(
        session,
        station_id=station_id,
        pollutant=pollutant,
        horizon_hours=horizon_hours,
        model_type=ModelType.CLASSIFIER,
        candidate_model_ids=[clf_model_id],
        candidate_f1=_f1(clf_metrics),
    )

    return registered_ids
