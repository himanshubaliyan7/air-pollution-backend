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
from sqlalchemy.orm import Session

from common.config import forecast_target, get_settings, load_yaml_config
from common.constants import ForecastTarget, ModelType, Pollutant
from db.models import ModelRun, Station
from db.readings import hourly_readings
from features.feature_store import get_feature_set_version, read_features
from models import evaluation, exceedance, level, registry
from models.daily import day_ahead_labels, local_day_means, monotone
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
    rows = hourly_readings(session, station_id, pollutant, start, end)  # same source rule as the features
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([t for t, _ in rows], tz="UTC")
    return pd.Series([v for _, v in rows], index=idx).groupby(level=0).first()


def _station_timezone(session: Session, station_id: str) -> str:
    from models.outlook import station_timezone  # local: outlook pulls in the serving-side modules

    station = session.get(Station, station_id)
    return station_timezone(station) if station is not None else "UTC"


def build_training_matrix(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    window_start: datetime,
    window_end: datetime,
    target: ForecastTarget | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """X = feature vectors already materialized in `features` (batch feature
    engineering), y = the realized label for `target` (default: the family
    being served): the pollutant value at feature_time + horizon, or the mean
    of the local day horizon / 24 days after feature_time's day.
    Rows with no realized label yet (too recent) or incomplete features are
    dropped - this is a supervised-learning matrix, not a serving path, so
    dropping NaNs here is correct (unlike build_feature_frame, which must
    tolerate them for cold-start stations)."""
    feature_frame = read_features(session, station_id, pollutant, window_start, window_end)
    if feature_frame.empty:
        return feature_frame, pd.Series(dtype="float64")

    if (target or forecast_target()) is ForecastTarget.DAILY_MEAN:
        tz_name = _station_timezone(session, station_id)
        # Two days past the horizon: the label day's own 24 hours, in any time zone.
        target_series = _fetch_target_series(
            session, station_id, pollutant, window_start, window_end + timedelta(hours=horizon_hours + 48)
        )
        labels = day_ahead_labels(feature_frame.index, local_day_means(target_series, tz_name), tz_name, horizon_hours // 24)
    else:
        target_start = window_start + timedelta(hours=horizon_hours)
        target_end = window_end + timedelta(hours=horizon_hours)
        target_series = _fetch_target_series(session, station_id, pollutant, target_start, target_end)
        labels = target_series.reindex(feature_frame.index + pd.Timedelta(hours=horizon_hours)).values

    combined = feature_frame.copy()
    combined["__label__"] = labels
    combined = combined.dropna()

    y = combined.pop("__label__")
    excluded = load_model_config()["training"].get("excluded_features", [])
    X = prepare_X(combined).drop(columns=excluded, errors="ignore")
    return X, y


def _train_level(session, station_id, pollutant, horizon_hours, window_start, window_end) -> list:
    """The daily-mean family (models/level.py): register this station's copy of
    the pooled ratio quantiles and activate it. Nothing is compared: the rule
    has no fitted state that a newer fit could make worse."""
    config = load_model_config()
    fitted = level.fit_log_ratio_quantiles(session, pollutant, window_start, window_end, sorted(config["quantiles"]))
    if horizon_hours not in fitted or fitted[horizon_hours][1] < config["training"]["min_training_rows"]:
        logger.warning("Not enough labelled rows for the %s level ratio at %dh - skipping", pollutant.value, horizon_hours)
        return []
    ratios, n_rows = fitted[horizon_hours]
    trained_at = datetime.now(timezone.utc)
    ids = []
    for q, value in ratios.items():
        model_id = registry.register_model_run(
            session, station_id=station_id, pollutant=pollutant, horizon_hours=horizon_hours,
            model_type=ModelType.QUANTILE_REGRESSOR, feature_set_version=get_feature_set_version(),
            artifact_path=f"{level.KIND}:{pollutant.value}:{horizon_hours}h:q{q}", trained_at=trained_at,
            training_window_start=window_start, training_window_end=window_end,
            metrics={"rows": n_rows}, hyperparams={"kind": level.KIND, "log_ratio": value, "pooled": True},
            quantile=q, target=ForecastTarget.DAILY_MEAN,
        )
        registry.activate_model(session, model_id)
        ids.append(model_id)
    logger.info("Level %s/%s/%dh: log ratios %s from %d rows", station_id, pollutant.value, horizon_hours,
                {q: round(v, 3) for q, v in ratios.items()}, n_rows)
    return ids


def train_station_pollutant_horizon(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    window_start: datetime,
    window_end: datetime,
    holdout_days: int,
    target: ForecastTarget | None = None,
) -> list:
    """Returns the list of newly-registered (not yet activated) model_run ids.
    `target` defaults to the family being served."""
    target = target or forecast_target()
    if target is ForecastTarget.DAILY_MEAN:
        return _train_level(session, station_id, pollutant, horizon_hours, window_start, window_end)
    config = load_model_config()
    quantiles = config["quantiles"]
    lgbm_cfg = config["lightgbm"]
    min_rows = config["training"]["min_training_rows"]

    holdout_start = window_end - timedelta(days=holdout_days)

    X, y = build_training_matrix(session, station_id, pollutant, horizon_hours, window_start, window_end, target)
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
        path = registry.artifact_path(
            station_id, pollutant, horizon_hours, ModelType.QUANTILE_REGRESSOR, trained_at, quantile=q, target=target
        )
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
            target=target,
        )
        registered_ids.append(model_id)
        quantile_model_ids[q] = model_id

    # Quantile-derived exceedance probability on the holdout set, for the
    # primary v1 signal's metrics.
    quantile_probs = np.array(
        [
            exceedance.probability_from_quantiles(
                monotone({q: quantile_holdout_preds[q][i] for q in quantiles}), threshold_conc
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
    clf_path = registry.artifact_path(station_id, pollutant, horizon_hours, ModelType.CLASSIFIER, trained_at, target=target)
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
        target=target,
    )
    registered_ids.append(clf_model_id)

    logger.info(
        "Trained %s/%s/%dh (%s): quantile-derived f1=%.3f, classifier f1=%.3f (holdout n=%d)",
        station_id, pollutant.value, horizon_hours, target.value,
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
        target=target,
    )
    registry.promote_if_better(
        session,
        station_id=station_id,
        pollutant=pollutant,
        horizon_hours=horizon_hours,
        model_type=ModelType.CLASSIFIER,
        candidate_model_ids=[clf_model_id],
        candidate_f1=_f1(clf_metrics),
        target=target,
    )

    return registered_ids
