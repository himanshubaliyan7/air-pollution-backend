"""Single source of truth for the database schema.

Every service (ingestion loaders, feature store, model registry, API,
alerting) imports its tables from here rather than redefining them, so the
schema never drifts between services. Alembic migrations under
db/migrations/ are generated from / kept in sync with this file; the initial
migration additionally promotes the time-heavy tables to TimescaleDB
hypertables via raw SQL (see db/migrations/versions/0001_initial_schema.py).

Uses SQLAlchemy 1.4-style legacy Column()/declarative_base() rather than
2.0's Mapped[]/mapped_column(), deliberately: this file is imported inside
the Airflow container too (orchestration/plugins/common/tasks.py), and
Airflow 2.9.3 bundles SQLAlchemy 1.4 - its own internal ORM models are not
SQLAlchemy-2.0-compatible (verified: forcing sqlalchemy>=2.0 in the Airflow
image crashes the scheduler with a MappedAnnotationError inside Airflow's
own TaskInstance model). Legacy Column syntax works unmodified under both
SQLAlchemy 1.4 (Airflow) and 2.0 (api/db-migrate/dashboard), keeping this
one schema file usable everywhere without a second, parallel definition.

Hypertable / primary-key note: TimescaleDB requires that any unique
constraint (including the primary key) on a hypertable include the
partitioning ("time") column. So each hypertable below uses a natural
composite primary key that includes its time column, rather than a bare
surrogate id. `alert_log` references a forecast by its natural key columns
(denormalized) instead of a formal foreign key, for the same reason.
"""

import uuid

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

from common.constants import (
    AlertStatus,
    ForecastTarget,
    ModelType,
    Pollutant,
    SensorSourceName,
    WeatherProductType,
)

Base = declarative_base()


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Station(Base):
    __tablename__ = "stations"

    station_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    city = Column(String, nullable=False, default="Delhi")
    state = Column(String, nullable=False, default="Delhi")
    source = Column(Enum(SensorSourceName, name="sensor_source_name"), nullable=False)
    # Source-side station id, e.g. OpenAQ location id.
    source_location_id = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class RawSensorReading(Base):
    """Hypertable, partitioned on observed_at."""

    __tablename__ = "raw_sensor_readings"

    station_id = Column(String, ForeignKey("stations.station_id"), primary_key=True)
    pollutant = Column(Enum(Pollutant, name="pollutant"), primary_key=True)
    observed_at = Column(DateTime(timezone=True), primary_key=True)
    source = Column(Enum(SensorSourceName, name="sensor_source_name_reading"), primary_key=True)
    value = Column(Float, nullable=False)
    unit = Column(String, nullable=False, default="ug/m3")
    source_record_id = Column(String, nullable=True)
    ingested_at = Column(DateTime(timezone=True), nullable=False)


class RawWeatherReading(Base):
    """Hypertable, partitioned on observed_at.

    ERA5T (preliminary, near-real-time) rows are kept alongside final ERA5
    rows for the same grid cell/timestamp rather than being overwritten -
    is_superseded marks an ERA5T row once the final ERA5 value has landed,
    so historical feature/training runs stay reproducible and debuggable.
    """

    __tablename__ = "raw_weather_readings"

    grid_cell_id = Column(String, primary_key=True)
    observed_at = Column(DateTime(timezone=True), primary_key=True)
    product_type = Column(Enum(WeatherProductType, name="weather_product_type"), primary_key=True)
    u_wind = Column(Float, nullable=False)
    v_wind = Column(Float, nullable=False)
    wind_speed = Column(Float, nullable=False)
    wind_direction = Column(Float, nullable=False)
    relative_humidity = Column(Float, nullable=False)
    is_superseded = Column(Boolean, nullable=False, default=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)


class Feature(Base):
    """Hypertable, partitioned on feature_time.

    feature_set_version ties a row to the exact version of the
    features/build_features.py transform logic that produced it, so training
    never silently mixes incompatible feature vectors.
    """

    __tablename__ = "features"

    station_id = Column(String, ForeignKey("stations.station_id"), primary_key=True)
    pollutant = Column(Enum(Pollutant, name="pollutant_feature"), primary_key=True)
    feature_time = Column(DateTime(timezone=True), primary_key=True)
    feature_set_version = Column(String, primary_key=True)
    features = Column(JSONB, nullable=False)
    computed_at = Column(DateTime(timezone=True), nullable=False)


class ModelRun(Base):
    """Model registry: metadata for a single trained artifact.

    is_active marks which row is currently served for a given
    (station, pollutant, horizon, model_type) - flipped by the retraining
    DAG's promote_if_better step, never by hand.
    """

    __tablename__ = "model_runs"

    model_id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    station_id = Column(String, ForeignKey("stations.station_id"), nullable=False)
    pollutant = Column(Enum(Pollutant, name="pollutant_model"), nullable=False)
    horizon_hours = Column(Integer, nullable=False)
    model_type = Column(Enum(ModelType, name="model_type"), nullable=False)
    quantile = Column(Float, nullable=True)
    feature_set_version = Column(String, nullable=False)
    artifact_path = Column(String, nullable=False)
    trained_at = Column(DateTime(timezone=True), nullable=False)
    training_window_start = Column(DateTime(timezone=True), nullable=False)
    training_window_end = Column(DateTime(timezone=True), nullable=False)
    metrics = Column(JSONB, nullable=False, default=dict)
    hyperparams = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=False)
    # common.constants.ForecastTarget. Part of the key is_active is unique over:
    # an hourly and a daily-mean model can both be active for one horizon.
    target = Column(String, nullable=False, default=ForecastTarget.HOURLY.value, server_default=ForecastTarget.HOURLY.value)


class Forecast(Base):
    """Hypertable, partitioned on target_time."""

    __tablename__ = "forecasts"

    station_id = Column(String, ForeignKey("stations.station_id"), primary_key=True)
    pollutant = Column(Enum(Pollutant, name="pollutant_forecast"), primary_key=True)
    model_id = Column(UUID(as_uuid=True), ForeignKey("model_runs.model_id"), primary_key=True)
    forecast_made_at = Column(DateTime(timezone=True), primary_key=True)
    target_time = Column(DateTime(timezone=True), primary_key=True)
    horizon_hours = Column(Integer, nullable=False)
    point_forecast = Column(Float, nullable=False)
    quantile_low = Column(Float, nullable=False)
    quantile_high = Column(Float, nullable=False)
    exceedance_probability = Column(Float, nullable=False)
    exceedance_flag = Column(Boolean, nullable=False)
    # common.constants.ForecastTarget: what point_forecast and the quantiles are
    # (a daily-mean row's target_time is the target day's local midnight).
    target = Column(String, nullable=False, default=ForecastTarget.HOURLY.value, server_default=ForecastTarget.HOURLY.value)


class ExceedanceEvaluation(Base):
    __tablename__ = "exceedance_evaluations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    station_id = Column(String, ForeignKey("stations.station_id"), nullable=False)
    pollutant = Column(Enum(Pollutant, name="pollutant_eval"), nullable=False)
    horizon_hours = Column(Integer, nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("model_runs.model_id"), nullable=False)
    evaluation_window_start = Column(DateTime(timezone=True), nullable=False)
    evaluation_window_end = Column(DateTime(timezone=True), nullable=False)
    precision = Column(Float, nullable=True)
    recall = Column(Float, nullable=True)
    f1 = Column(Float, nullable=True)
    mae = Column(Float, nullable=True)
    rmse = Column(Float, nullable=True)
    n_exceedance_days_actual = Column(Integer, nullable=False, default=0)
    n_exceedance_days_predicted = Column(Integer, nullable=False, default=0)
    computed_at = Column(DateTime(timezone=True), nullable=False)


class AlertSubscription(Base):
    """One row per email address (owner-verified double opt-in).

    Alerts go only to rows that are BOTH is_confirmed and is_active. A row
    starts pending (is_confirmed=False, is_active=False) and only the emailed
    confirm token activates it. is_confirmed records that the address owner
    proved control of the mailbox and never goes back to False; unsubscribing
    only flips is_active. Emailed tokens (confirm, manage) are stored solely as
    SHA-256 hashes; see alerting/tokens.py.
    """

    __tablename__ = "alert_subscriptions"

    subscriber_id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email = Column(String, nullable=False, unique=True)
    station_ids = Column(ARRAY(String), nullable=False)
    pollutants = Column(ARRAY(String), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    is_confirmed = Column(Boolean, nullable=False, default=False, server_default="false")
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    # Single-use; cleared on confirmation.
    confirm_token_hash = Column(String, nullable=True, unique=True)
    confirm_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    # Multi-use until expiry; lets the confirmed owner change or stop the subscription.
    manage_token_hash = Column(String, nullable=True, unique=True)
    manage_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    # Per-email rate limit for confirm/manage emails: count within the window
    # that began at email_window_started_at.
    email_window_started_at = Column(DateTime(timezone=True), nullable=True)
    email_send_count = Column(Integer, nullable=False, default=0, server_default="0")
    # When the owner switched alerts off; retention deletes the row 90 days
    # later (alerting/retention.py). Cleared when they switch alerts back on.
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)

    alert_log_entries = relationship("AlertLog", back_populates="subscriber")


class AlertLog(Base):
    """References a forecast by its natural key columns (not a formal FK -
    see module docstring) since `forecasts` has no bare-id unique key."""

    __tablename__ = "alert_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    subscriber_id = Column(UUID(as_uuid=True), ForeignKey("alert_subscriptions.subscriber_id"), nullable=False)
    station_id = Column(String, nullable=False)
    pollutant = Column(Enum(Pollutant, name="pollutant_alert"), nullable=False)
    forecast_model_id = Column(UUID(as_uuid=True), nullable=False)
    forecast_made_at = Column(DateTime(timezone=True), nullable=False)
    target_time = Column(DateTime(timezone=True), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum(AlertStatus, name="alert_status"), nullable=False)
    error_detail = Column(Text, nullable=True)

    subscriber = relationship("AlertSubscription", back_populates="alert_log_entries")

    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "station_id",
            "pollutant",
            "forecast_model_id",
            "forecast_made_at",
            "target_time",
            name="uq_alert_log_idempotency",
        ),
    )


class StationAqiSnapshot(Base):
    """One hourly CPCB AQI SUB-INDEX reading per station and pollutant, from the
    data.gov.in real-time feed (see ingestion/sources/data_gov_in.py). These are
    sub-indices over a rolling window, NOT hourly ug/m3 concentrations, so they
    live apart from raw_sensor_readings and never feed the forecasting models.
    Every hourly snapshot is kept: the feed has no history of its own."""

    __tablename__ = "station_aqi_snapshots"

    station_id = Column(String, ForeignKey("stations.station_id"), primary_key=True)
    pollutant_id = Column(String, primary_key=True)  # as published: PM2.5, PM10, NO2, SO2, CO, OZONE, NH3
    source_updated_at = Column(DateTime(timezone=True), primary_key=True)
    sub_index_min = Column(Float, nullable=True)
    sub_index_max = Column(Float, nullable=True)
    sub_index_avg = Column(Float, nullable=True)
    sub_index_hourly = Column(Float, nullable=True)  # direct CPCB feed only; null from data.gov.in
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    source = Column(String, nullable=True)  # "cpcb-caaqms" or "data-gov-in"; null before 2026-09-28


HYPERTABLE_SPECS = [
    # (table_name, time_column, chunk_time_interval)
    ("raw_sensor_readings", "observed_at", "7 days"),
    ("raw_weather_readings", "observed_at", "7 days"),
    ("features", "feature_time", "7 days"),
    ("forecasts", "target_time", "7 days"),
]


class DigestLog(Base):
    """One row per daily digest attempt. The unique (subscriber, digest_date)
    key makes a retried digest task unable to email anyone twice for a day."""

    __tablename__ = "digest_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    subscriber_id = Column(UUID(as_uuid=True), ForeignKey("alert_subscriptions.subscriber_id"), nullable=False)
    digest_date = Column(Date, nullable=False)  # the local day the digest is about
    sent_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum(AlertStatus, name="alert_status"), nullable=False)  # type shared with alert_log
    error_detail = Column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("subscriber_id", "digest_date", name="uq_digest_log_subscriber_day"),)
