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
    """Undefined metrics are None, not 0: a window with no real exceedances
    has no recall to measure, and scoring it 0.0 made every clean-air day
    look like total model failure to the drift check."""
    n_actual = int(np.sum(y_true_binary))
    n_predicted = int(np.sum(y_pred_binary))
    return {
        "precision": float(precision_score(y_true_binary, y_pred_binary, zero_division=0)) if n_predicted else None,
        "recall": float(recall_score(y_true_binary, y_pred_binary, zero_division=0)) if n_actual else None,
        "f1": float(f1_score(y_true_binary, y_pred_binary, zero_division=0)) if n_actual or n_predicted else None,
        "n_exceedance_days_actual": n_actual,
        "n_exceedance_days_predicted": n_predicted,
    }


def recall_drift_detected(recalls: list, min_recall: float) -> bool:
    """True when most recent evaluations that could measure recall fall
    below min_recall. Evaluations with undefined recall (None) are ignored."""
    defined = [r for r in recalls if r is not None]
    low = [r for r in defined if r < min_recall]
    return bool(defined) and len(low) > len(defined) / 2


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, quantile: float) -> float:
    diff = y_true - y_pred
    return float(np.mean(np.maximum(quantile * diff, (quantile - 1) * diff)))
