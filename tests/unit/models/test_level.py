import math

import numpy as np

from models.level import forecast_values, log_ratio


def test_the_forecast_is_the_last_24h_mean_times_the_ratio():
    values = forecast_values(80.0, {0.1: math.log(0.5), 0.5: 0.0, 0.9: math.log(2.0)})
    assert values == {0.1: 40.0, 0.5: 80.0, 0.9: 160.0}


def test_a_zero_reading_keeps_the_ratio_finite():
    assert np.isfinite(log_ratio(np.array([0.0, 50.0]), np.array([20.0, 0.0]))).all()
    assert forecast_values(0.0, {0.5: 0.0}) == {0.5: 1.0}
