import enum

IST_OFFSET_HOURS = 5.5  # India Standard Time, UTC+5:30, no DST


class Pollutant(str, enum.Enum):
    PM25 = "pm25"
    NO2 = "no2"


class SensorSourceName(str, enum.Enum):
    OPENAQ = "openaq"
    AIRNOW = "airnow"  # reserved for a future US region, not wired up for Delhi NCR


class WeatherProductType(str, enum.Enum):
    ERA5 = "era5"
    ERA5T = "era5t"
    # Open-Meteo near-term forecast (ingestion/weather/open_meteo_client.py).
    # ERA5 has a real ~5 day publication lag even for its ERA5T preliminary
    # extension (verified live) - there is a genuine window (roughly "now"
    # back to ~5 days ago) where NO ERA5 data exists yet at any quality
    # level. FORECAST fills exactly that gap for near-real-time inference;
    # it is never used for training (build_feature_frame prefers
    # ERA5 > ERA5T > FORECAST, and training only reads materialized
    # `features` rows built from whatever was available at computation
    # time, which for historical windows is always real ERA5/ERA5T).
    FORECAST = "forecast"


class ModelType(str, enum.Enum):
    QUANTILE_REGRESSOR = "quantile_regressor"
    POINT_REGRESSOR = "point_regressor"
    CLASSIFIER = "classifier"


class AlertStatus(str, enum.Enum):
    SENT = "sent"
    FAILED = "failed"


# Direct multi-horizon forecast targets, in hours ahead.
DEFAULT_HORIZONS_HOURS = [24, 48, 72, 96, 120]

# Quantiles fitted per horizon; used both for uncertainty bands and for
# interpolating P(value > threshold) in models/exceedance.py.
DEFAULT_QUANTILES = [0.1, 0.5, 0.9]

LAG_HOURS = [1, 3, 6, 12, 24, 48]
ROLLING_WINDOWS_HOURS = [6, 24, 48]
