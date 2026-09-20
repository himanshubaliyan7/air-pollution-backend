"""LightGBM fitting for the three model_type variants: quantile regressor,
point regressor, and the comparison exceedance classifier. All three train
on the same X - only the objective/target differs.
"""

import lightgbm as lgb
import numpy as np
import pandas as pd


def prepare_X(feature_frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce every column to numeric (LightGBM rejects non-numeric dtypes).

    Bool feature columns (is_diwali_window, is_stubble_season) round-trip
    through the `features` table's JSONB storage as plain Python True/False/
    None mixed together, which pandas infers as dtype=object (not dtype=bool)
    when the frame is built from a list of dicts - a dtype==bool check alone
    misses these, so every column is coerced via pd.to_numeric regardless of
    its original dtype (True/False -> 1.0/0.0, None -> NaN, numerics pass
    through unchanged).
    """
    X = feature_frame.copy()
    for col in X.columns:
        X[col] = pd.to_numeric(X[col], errors="coerce")
    return X


def fit_quantile_model(X: pd.DataFrame, y: pd.Series, quantile: float, params: dict) -> lgb.Booster:
    train_set = lgb.Dataset(X, label=y)
    full_params = {
        "objective": "quantile",
        "alpha": quantile,
        "metric": "quantile",
        "verbosity": -1,
        **params,
    }
    n_estimators = full_params.pop("n_estimators", 200)
    return lgb.train(full_params, train_set, num_boost_round=n_estimators)


def fit_point_model(X: pd.DataFrame, y: pd.Series, params: dict) -> lgb.Booster:
    train_set = lgb.Dataset(X, label=y)
    full_params = {"objective": "regression", "metric": "rmse", "verbosity": -1, **params}
    n_estimators = full_params.pop("n_estimators", 200)
    return lgb.train(full_params, train_set, num_boost_round=n_estimators)


def fit_classifier(X: pd.DataFrame, y_binary: pd.Series, params: dict) -> lgb.Booster:
    train_set = lgb.Dataset(X, label=y_binary)
    full_params = {"objective": "binary", "metric": "binary_logloss", "verbosity": -1, **params}
    n_estimators = full_params.pop("n_estimators", 200)
    is_unbalance = full_params.pop("is_unbalance", True)
    full_params["is_unbalance"] = is_unbalance
    return lgb.train(full_params, train_set, num_boost_round=n_estimators)


def predict(booster: lgb.Booster, X: pd.DataFrame) -> np.ndarray:
    return booster.predict(X)
