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

    # No default on purpose: a credential-shaped default in code ends up in git history and in
    # secret scanners. Set DATABASE_URL in the environment (docker/.env, or TEST_DATABASE_URL for tests).
    database_url: str = ""

    # Origins a browser frontend may call the API from, comma-separated, or "*".
    # The API is unauthenticated and sets no cookies, so "*" exposes nothing
    # extra today; restrict it to the real frontend origin(s) once known.
    cors_allowed_origins: str = "*"

    # Public URL of the dashboard/frontend, linked from alert emails. Must be
    # set for real deployments: recipients cannot open the localhost default.
    dashboard_url: str = "http://localhost:8501"

    # --- alert subscriptions (owner-verified double opt-in) ---
    # Public base URL of the frontend that hosts the confirm / manage /
    # unsubscribe pages linked from emails (<base>/subscriptions/confirm?token=...).
    # Empty means "use dashboard_url". Must be a real, recipient-reachable URL
    # in any deployment.
    frontend_base_url: str = ""
    # Public base URL of THIS API, used for the RFC 8058 one-click unsubscribe
    # URL in alert emails (mail providers POST to it directly, so it must be
    # the API and must be https in production). api_base_url is the internal
    # docker-network address and is useless to a mail provider.
    public_api_base_url: str = "http://localhost:8000"
    # Lifetime of emailed confirm / manage tokens.
    subscription_token_ttl_hours: int = 48
    # Max confirmation/manage emails sent to one address per rolling hour-window.
    subscription_emails_per_hour: int = 3
    # Key for the stateless per-subscriber unsubscribe token embedded in alert
    # emails (they must work indefinitely, and the plaintext cannot be
    # recovered from a stored hash). Required to send or check them; generate
    # once with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
    # and never rotate casually (rotation breaks every emailed link).
    subscription_token_secret: str = ""
    # Demo mode: when set (comma-separated addresses), only these may subscribe
    # or receive the digest. Empty = open to anyone. Chosen by the owner
    # 2026-09-28 for the non-commercial portfolio deployment.
    subscription_allowed_emails: str = ""

    def subscription_allowlist(self) -> frozenset[str] | None:
        """Lowercased invited addresses, or None when sign-ups are open."""
        emails = {e.strip().lower() for e in self.subscription_allowed_emails.split(",") if e.strip()}
        return frozenset(emails) or None

    def may_subscribe(self, email: str) -> bool:
        allowlist = self.subscription_allowlist()
        return allowlist is None or email.strip().lower() in allowlist

    openaq_api_key: str = ""
    # Free personal key from data.gov.in (My Account). Empty = current-AQI ingestion is skipped.
    data_gov_in_api_key: str = ""

    # Operator alerts (Telegram) and a dead-man's-switch ping URL (e.g. healthchecks.io).
    # All optional: empty means the alert path is a silent no-op.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    healthchecks_ping_url: str = ""

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
