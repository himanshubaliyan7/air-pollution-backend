from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.constants import IST_OFFSET_HOURS

REPO_ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=IST_OFFSET_HOURS))


class Settings(BaseSettings):
    """Secrets and environment-specific values, loaded from process env / .env.

    Non-secret, human-editable config (station lists, thresholds, feature
    windows) lives in YAML under config/ and is loaded separately via the
    load_yaml_config() helper below, not through this class, so operators can
    edit it without touching environment variables or rebuilding images.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/airpollution"

    # Origins a browser frontend may call the API from, comma-separated, or "*".
    # The API is unauthenticated and sets no cookies, so "*" exposes nothing
    # extra today; restrict it to the real frontend origin(s) once known.
    cors_allowed_origins: str = "*"

    # Public URL of the dashboard/frontend, linked from alert emails. Must be
    # set for real deployments: recipients cannot open the localhost default.
    dashboard_url: str = "http://localhost:8501"

    openaq_api_key: str = ""

    cds_api_url: str = "https://cds.climate.copernicus.eu/api"
    cds_api_key: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_from_address: str = "alerts@air-pollution-prediction.local"

    api_base_url: str = "http://api:8000"

    stations_config_path: Path = REPO_ROOT / "config" / "stations_delhi_ncr.yaml"
    # AQI_THRESHOLD_CONFIG_PATH (not THRESHOLDS_CONFIG_PATH) is the documented
    # env var name (docker/env/.env.example, docker-compose.yml) - aliased
    # explicitly since it doesn't match pydantic-settings' default
    # upper-cased-field-name convention.
    thresholds_config_path: Path = Field(
        default=REPO_ROOT / "config" / "thresholds_cpcb.yaml", validation_alias="AQI_THRESHOLD_CONFIG_PATH"
    )
    settings_config_path: Path = REPO_ROOT / "config" / "settings.yaml"
    model_config_path: Path = REPO_ROOT / "models" / "config" / "model_config.yaml"

    model_artifacts_dir: Path = REPO_ROOT / "models" / "artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """Convert an aware UTC datetime to IST for presentation / day-bucketing.

    All storage and internal computation stays in UTC; this is only for the
    presentation boundary (dashboard) and for calendar-day bucketing logic
    (e.g. daily exceedance aggregation) where a naive UTC-day split would
    misassign late-night IST hours to the wrong calendar day.
    """
    if dt.tzinfo is None:
        raise ValueError("to_ist() requires a timezone-aware datetime (expected UTC)")
    return dt.astimezone(IST)


def ist_calendar_date(dt: datetime):
    return to_ist(dt).date()
