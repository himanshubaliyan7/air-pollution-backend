"""Join wind/humidity weather features onto a station's pollutant time series.

Deliberately indifferent to ERA5 vs ERA5T (RawWeatherReading.product_type) -
by the time training happens, reconcile_era5_final will have overwritten
most ERA5T rows with final ERA5; inference necessarily uses ERA5T for the
most recent ~5 days regardless. This is an accepted, documented tradeoff
(see ingestion/weather/era5_client.py), not something this module works
around.
"""

import pandas as pd


def join_weather(pollutant_df: pd.DataFrame, weather_df: pd.DataFrame) -> pd.DataFrame:
    """pollutant_df: hourly-indexed (UTC) frame for one station/pollutant.
    weather_df: hourly-indexed (UTC) frame for the station's grid cell, with
    columns wind_speed, wind_direction, relative_humidity - already reduced
    to one row per hour (see feature_store's superseded-row filtering)."""
    return pollutant_df.join(
        weather_df[["wind_speed", "wind_direction", "relative_humidity"]], how="left"
    )
