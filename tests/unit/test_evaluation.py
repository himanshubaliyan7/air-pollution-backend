import numpy as np

from models.evaluation import (
    exceedance_classification_metrics,
    missed_days_drift,
    pooled_recall,
    recall_drift_detected,
)


def test_clean_air_window_has_undefined_not_zero_metrics():
    # No real and no predicted exceedances: the model was right, not failing.
    m = exceedance_classification_metrics(np.zeros(24, dtype=int), np.zeros(24, dtype=int))
    assert m["precision"] is None
    assert m["recall"] is None
    assert m["f1"] is None
    assert m["n_exceedance_days_actual"] == 0


def test_missed_exceedances_score_zero_recall():
    y_true = np.array([1, 1, 0, 0])
    m = exceedance_classification_metrics(y_true, np.zeros(4, dtype=int))
    assert m["recall"] == 0.0
    assert m["precision"] is None
    assert m["f1"] == 0.0


def test_false_alarms_only_leave_recall_undefined():
    m = exceedance_classification_metrics(np.zeros(4, dtype=int), np.array([1, 0, 0, 0]))
    assert m["recall"] is None
    assert m["precision"] == 0.0
    assert m["f1"] == 0.0


def test_drift_ignores_undefined_recall():
    assert not recall_drift_detected([None] * 20, 0.4)
    assert not recall_drift_detected([], 0.4)


def test_drift_detected_when_most_defined_recalls_low():
    assert recall_drift_detected([0.1, 0.2, 0.9, None, None], 0.4)
    assert not recall_drift_detected([0.1, 0.9, 0.8, None], 0.4)


def test_pooled_recall_weights_each_evaluation_by_its_exceedance_days():
    # 3 of 4 one-day evaluations caught, plus a 10-day window that caught half.
    rows = [(1, 1.0), (1, 1.0), (1, 1.0), (1, 0.0), (10, 0.5)]
    recall, days = pooled_recall(rows)
    assert days == 14
    assert recall == 8 / 14


def test_pooled_recall_is_undefined_without_real_exceedances():
    assert pooled_recall([]) == (None, 0)
    assert pooled_recall([(0, None), (0, None)]) == (None, 0)


def test_daily_drift_needs_enough_days_before_it_fires():
    missed_week = [(1, 0.0)] * 19
    assert not missed_days_drift(missed_week, min_recall=0.5, min_days=20)
    assert missed_days_drift(missed_week + [(1, 0.0)], min_recall=0.5, min_days=20)


def test_daily_drift_passes_when_most_bad_days_were_flagged():
    rows = [(1, 1.0)] * 15 + [(1, 0.0)] * 5 + [(0, None)] * 400
    assert not missed_days_drift(rows, min_recall=0.5, min_days=20)
    assert missed_days_drift([(1, 1.0)] * 9 + [(1, 0.0)] * 11, min_recall=0.5, min_days=20)
