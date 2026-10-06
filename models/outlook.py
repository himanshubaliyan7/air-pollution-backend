"""The school-facing go/caution/no-go outlook for one station and pollutant.

Shared by GET /forecast/{id}/exceedance and the daily alert digest, so the
website and the email can never disagree about a day.

Which forecasts it reads depends on the model family being served
(common.config.forecast_target):

- daily_mean (owner decision 2026-10-02): each row forecasts the mean of one
  station-local calendar day. The day's category is the worst AQI category that
  mean reaches with the configured probability, and the verdict follows from
  the category: go below the health threshold, caution in the health-threshold
  category, no-go from thresholds["no_go_category"] (models/daily.py).
- hourly: each horizon (24 h, 48 h, ...) targets a single future hour, so one
  forecast hour stands in for its calendar day, and the day is no-go when that
  hour is likely above the health threshold.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.aqi import sub_index_from_concentration
from common.config import forecast_target
from common.constants import DEFAULT_HORIZONS_HOURS, DEFAULT_QUANTILES, MAX_INPUT_STALENESS_HOURS, ForecastTarget, Pollutant
from common.freshness import floor_hour, is_input_fresh
from common.regions import region_for_point
from db.models import Forecast, Station
from models.daily import category_for_day, verdict_for_category
from models.exceedance import get_aqi_category, thresholds_for_point

CAUTION_PROBABILITY_FLOOR = 0.15  # hourly family: below the decision threshold but worth flagging as "caution"


@dataclass(frozen=True)
class OutlookDay:
    date: date
    exceedance_flag: bool
    exceedance_probability: float
    expected_value: float
    worst_case_value: float
    aqi_category: str
    verdict: str  # "go" | "caution" | "no-go"
    # CPCB sub-index of expected_value for this pollutant alone. Not an overall
    # AQI: that is the worst of several pollutants, most of them not forecast.
    expected_sub_index: int


def _hourly_day(day: date, r: Forecast, thresholds: dict, pollutant: Pollutant) -> OutlookDay:
    if r.exceedance_flag:
        verdict = "no-go"
    elif r.exceedance_probability >= CAUTION_PROBABILITY_FLOOR:
        verdict = "caution"
    else:
        verdict = "go"
    return OutlookDay(
        date=day,
        exceedance_flag=r.exceedance_flag,
        exceedance_probability=r.exceedance_probability,
        expected_value=r.point_forecast,
        worst_case_value=r.quantile_high,
        aqi_category=get_aqi_category(thresholds, pollutant, r.quantile_high),
        verdict=verdict,
        expected_sub_index=sub_index_from_concentration(thresholds, pollutant.value, r.point_forecast),
    )


def _daily_mean_day(day: date, r: Forecast, thresholds: dict, pollutant: Pollutant) -> OutlookDay:
    # predict.forecast stores the lowest, median and highest fitted quantile.
    quantiles = dict(zip((min(DEFAULT_QUANTILES), 0.5, max(DEFAULT_QUANTILES)),
                         (r.quantile_low, r.point_forecast, r.quantile_high)))
    category = category_for_day(quantiles, thresholds, pollutant)
    return OutlookDay(
        date=day,
        exceedance_flag=r.exceedance_flag,
        exceedance_probability=r.exceedance_probability,
        expected_value=r.point_forecast,
        worst_case_value=r.quantile_high,
        aqi_category=category,
        verdict=verdict_for_category(category, thresholds, pollutant),
        expected_sub_index=sub_index_from_concentration(thresholds, pollutant.value, r.point_forecast),
    )


@dataclass(frozen=True)
class Outlook:
    station_id: str
    pollutant: str
    timezone: str
    forecast_made_at: datetime | None
    is_current: bool
    days: list[OutlookDay]
    overall_recommendation: str  # "go" | "caution" | "no-go" | "no-data"

    def verdict_for(self, day: date) -> str:
        """A single day's verdict; a day without a current forecast is no-data, never go."""
        if not self.is_current:
            return "no-data"
        match = next((d for d in self.days if d.date == day), None)
        return match.verdict if match else "no-data"


def station_timezone(station: Station) -> str:
    region = region_for_point(station.lat, station.lon)
    return region.timezone if region else "UTC"


def latest_forecast_made_at(session: Session, station_id: str, pollutant: Pollutant) -> datetime | None:
    """Newest run of the model family being served."""
    return session.execute(
        select(func.max(Forecast.forecast_made_at)).where(
            Forecast.station_id == station_id, Forecast.pollutant == pollutant,
            Forecast.target == forecast_target().value,
        )
    ).scalar_one_or_none()


def is_forecast_current(made_at: datetime | None, now: datetime | None = None) -> bool:
    """A forecast is only actionable while its anchor hour is fresh by the same
    rule generate_forecasts uses (common.freshness); older, it must be treated
    as no forecast at all, never as an implicit "go"."""
    return made_at is not None and is_input_fresh(made_at, now or datetime.now(timezone.utc))


def build_outlook(
    session: Session, station: Station, pollutant: Pollutant, days_ahead: int = 5, now: datetime | None = None
) -> Outlook:
    made_at = latest_forecast_made_at(session, station.station_id, pollutant)
    rows = []
    if is_forecast_current(made_at, now):
        rows = session.execute(
            select(Forecast)
            .where(
                Forecast.station_id == station.station_id,
                Forecast.pollutant == pollutant,
                Forecast.forecast_made_at == made_at,
                Forecast.target == forecast_target().value,
                Forecast.horizon_hours <= days_ahead * 24,
            )
            .order_by(Forecast.target_time)
        ).scalars().all()
    return outlook_from_rows(station, pollutant, made_at, rows, days_ahead, now)


def build_outlooks(
    session: Session, stations: list[Station], days_ahead: int = 5, now: datetime | None = None
) -> dict[tuple[str, Pollutant], Outlook]:
    """build_outlook for every station and pollutant in two queries (the
    overview endpoint: one request instead of two per station). Only current
    runs are looked up, so a pair without one is no-data with forecast_made_at
    None, where build_outlook would report how old its last run is."""
    now = now or datetime.now(timezone.utc)
    oldest_current = floor_hour(now) - timedelta(hours=MAX_INPUT_STALENESS_HOURS)
    recent = (
        Forecast.forecast_made_at >= oldest_current,
        Forecast.target_time >= oldest_current,  # the hypertable's partition column: scan recent chunks only
        Forecast.target == forecast_target().value,
    )
    made_at = {
        (sid, pol): ts
        for sid, pol, ts in session.execute(
            select(Forecast.station_id, Forecast.pollutant, func.max(Forecast.forecast_made_at))
            .where(*recent)
            .group_by(Forecast.station_id, Forecast.pollutant)
        )
    }
    rows: dict[tuple[str, Pollutant], list[Forecast]] = {}
    for r in session.execute(
        select(Forecast).where(*recent, Forecast.horizon_hours <= days_ahead * 24).order_by(Forecast.target_time)
    ).scalars():
        key = (r.station_id, r.pollutant)
        if r.forecast_made_at == made_at.get(key):
            rows.setdefault(key, []).append(r)
    return {
        (s.station_id, pol): outlook_from_rows(
            s, pol, made_at.get((s.station_id, pol)), rows.get((s.station_id, pol), []), days_ahead, now
        )
        for s in stations
        for pol in Pollutant
    }


def outlook_from_rows(
    station: Station, pollutant: Pollutant, made_at: datetime | None, rows: list[Forecast], days_ahead: int = 5,
    now: datetime | None = None,
) -> Outlook:
    """The decision itself, from the newest run's rows (`made_at` is that run's
    anchor hour; `rows` its forecasts up to `days_ahead`)."""
    tz_name = station_timezone(station)
    # No forecast, or one anchored on input older than generate_forecasts is
    # willing to use, must never read as "go" - a school would treat silence
    # as clearance.
    if not is_forecast_current(made_at, now):
        return Outlook(station.station_id, pollutant.value, tz_name, made_at, False, [], "no-data")

    thresholds = thresholds_for_point(station.lat, station.lon)
    by_day: dict = {}
    for r in rows:
        local_date = r.target_time.astimezone(ZoneInfo(tz_name)).date()
        existing = by_day.get(local_date)
        # If more than one horizon lands on the same local day, keep the
        # worst-case (highest exceedance probability) one.
        if existing is None or r.exceedance_probability > existing.exceedance_probability:
            by_day[local_date] = r

    describe = _daily_mean_day if forecast_target() is ForecastTarget.DAILY_MEAN else _hourly_day
    days = [describe(d, r, thresholds, pollutant) for d, r in sorted(by_day.items())]

    # predict.forecast can skip individual horizons (missing model, gap in lag
    # history), so the newest run may cover only some days. Known bad days still
    # mean no-go, but an incomplete run with nothing flagged must not read as go.
    expected = {h for h in DEFAULT_HORIZONS_HOURS if h <= days_ahead * 24}
    incomplete = not expected <= {r.horizon_hours for r in rows}

    verdicts = {d.verdict for d in days}
    if "no-go" in verdicts:
        recommendation = "no-go"
    elif incomplete:
        recommendation = "no-data"
    elif "caution" in verdicts:
        recommendation = "caution"
    else:
        recommendation = "go"
    return Outlook(station.station_id, pollutant.value, tz_name, made_at, True, days, recommendation)
