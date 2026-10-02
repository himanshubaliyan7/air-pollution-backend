"""Real-time inference entrypoint - used by both forecast_dag and the API's
on-demand paths (if any). Calls the exact same features.build_feature_frame
used by batch feature engineering, so there is no train/serve skew.

Uses whichever model_type is currently is_active per (station, pollutant,
horizon) - v1 ships with QUANTILE_REGRESSOR active (see models/train.py's
promote_if_better bootstrap: first-ever training run always activates,
since there's nothing to compare against yet). A later retraining cycle can
activate the CLASSIFIER instead without any change here: forecasts.
exceedance_probability/exceedance_flag are populated the same way regardless
of which model_type produced them.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from common.constants import ModelType, Pollutant
from features.build_features import build_feature_frame
from models import exceedance, registry
from models.lightgbm_pipeline import predict as lgbm_predict
from models.lightgbm_pipeline import prepare_X


@dataclass(frozen=True)
class ForecastResult:
    model_id: object  # uuid.UUID
    point_forecast: float
    quantile_low: float
    quantile_high: float
    exceedance_probability: float
    exceedance_flag: bool


def forecast(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    horizon_hours: int,
    as_of_time: datetime,
    station_lat: float,
    station_lon: float,
    thresholds: dict | None = None,
) -> ForecastResult | None:
    thresholds = thresholds or exceedance.load_thresholds()
    threshold_conc = exceedance.get_health_threshold_concentration(pollutant, thresholds)

    quantile_models = registry.get_active_models(session, station_id, pollutant, horizon_hours, ModelType.QUANTILE_REGRESSOR)
    if not quantile_models:
        return None  # not trained yet for this key

    feature_frame = build_feature_frame(
        session, station_id, pollutant, [as_of_time], station_lat=station_lat, station_lon=station_lon
    )
    if feature_frame.empty or feature_frame.isna().all(axis=None):
        return None  # cold-start station, nothing to predict from

    X = prepare_X(feature_frame)
    if X.isna().any(axis=None):
        return None  # incomplete lag history for this as_of_time

    quantile_preds: dict[float, float] = {}
    median_model_id = None
    for row in quantile_models:
        try:
            booster = registry.load_booster(row.artifact_path)
        except Exception as exc:  # noqa: BLE001 - any load failure (missing file, corrupt LightGBM model, permissions) is treated the same: skip this one model, don't take down the whole forecast run
            # A missing/corrupt artifact for one station/horizon must not
            # take down forecasting for every other station in the same
            # forecast_dag run - log loudly and skip just this one.
            logger.error(
                "Could not load model artifact %s for %s/%s/%dh: %s",
                row.artifact_path, station_id, pollutant.value, horizon_hours, exc,
            )
            continue
        # Each model gets the columns it was trained on: models from before a
        # change to training.excluded_features keep working until retrained.
        pred = float(lgbm_predict(booster, X[booster.feature_name()])[0])
        quantile_preds[row.quantile] = pred
        if row.quantile == 0.5:
            median_model_id = row.model_id

    if not quantile_preds:
        return None

    probability = exceedance.probability_from_quantiles(quantile_preds, threshold_conc)
    flag = exceedance.classify_exceedance(probability, thresholds=thresholds)

    return ForecastResult(
        model_id=median_model_id or quantile_models[0].model_id,
        point_forecast=quantile_preds.get(0.5, sorted(quantile_preds.values())[len(quantile_preds) // 2]),
        quantile_low=min(quantile_preds.values()),
        quantile_high=max(quantile_preds.values()),
        exceedance_probability=probability,
        exceedance_flag=flag,
    )
