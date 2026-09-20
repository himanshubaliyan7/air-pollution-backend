"""Evaluation metrics. Precision/recall/F1 on exceedance-day classification
is the PRIMARY metric this system is judged on (see plan Context); MAE/RMSE
on point forecasts are secondary, mostly useful for the Model Health
dashboard and for calibration sanity-checking the quantile models feeding
models/exceedance.py's probability_from_quantiles.
"""

import numpy as np
from sklearn.metrics import f1_score, mean_absolute_error, precision_score, recall_score
from sklearn.metrics import mean_squared_error


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
    }


def exceedance_classification_metrics(y_true_binary: np.ndarray, y_pred_binary: np.ndarray) -> dict:
    return {
        "precision": float(precision_score(y_true_binary, y_pred_binary, zero_division=0)),
        "recall": float(recall_score(y_true_binary, y_pred_binary, zero_division=0)),
        "f1": float(f1_score(y_true_binary, y_pred_binary, zero_division=0)),
        "n_exceedance_days_actual": int(np.sum(y_true_binary)),
        "n_exceedance_days_predicted": int(np.sum(y_pred_binary)),
    }


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, quantile: float) -> float:
    diff = y_true - y_pred
    return float(np.mean(np.maximum(quantile * diff, (quantile - 1) * diff)))
