"""Read-only: are implausible sensor readings feeding the models?

Written for the 2026-10-02 finding that the public API served PM2.5 forecasts
of thousands of ug/m3 (Shadipur: point forecast 8,243 where the 90-day observed
maximum is 205). Nothing between OpenAQ/CPCB and training rejects a value, so
an instrument fault in the training window becomes a label. This prints:

1. per pollutant and source, how many readings in the training window are
   negative or above each level;
2. the stations with the highest readings, and when those were recorded;
3. the current forecasts above the first level, next to that station's own
   readings, to show whether the wild forecasts sit on the stations with wild
   readings.

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/reading_outlier_diagnosis.py
    ... python - --top 40 < scripts/reading_outlier_diagnosis.py
"""

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from common.constants import MAX_INPUT_STALENESS_HOURS, Pollutant
from db.models import Forecast, RawSensorReading, Station
from db.session import get_session
from models.train import load_model_config

# ug/m3. The first level is where a forecast is listed in part 3. PM2.5 monitors
# (BAM) measure up to about 1,000; Delhi's worst hours on record are near it.
LEVELS = {Pollutant.PM25: (500, 1000, 2000, 5000), Pollutant.NO2: (400, 1000, 2000, 5000)}


def _t(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=25, help="stations to list per pollutant in part 2")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=load_model_config()["training"]["default_training_window_days"])
    value = RawSensorReading.value
    in_window = RawSensorReading.observed_at >= window_start
    session = get_session()
    try:
        names = dict(session.execute(select(Station.station_id, Station.name)).all())
        print(f"as of {_t(now)} UTC; training window starts {_t(window_start)}; values in ug/m3, times in UTC")

        print("\n1. readings in the training window: pollutant source | n | negative | "
              + " | ".join(f"> {level}" for level in LEVELS[Pollutant.PM25]) + " (pm25 levels; no2 uses "
              + ", ".join(str(level) for level in LEVELS[Pollutant.NO2]) + ") | max")
        for pollutant in Pollutant:
            for source, n, negative, *rest in session.execute(
                select(RawSensorReading.source, func.count(), func.count().filter(value < 0),
                       *[func.count().filter(value > level) for level in LEVELS[pollutant]], func.max(value))
                .where(in_window, RawSensorReading.pollutant == pollutant)
                .group_by(RawSensorReading.source)
            ):
                *above, highest = rest
                print(f"  {pollutant.value:5s} {source.value:7s} | {n} | {negative} | "
                      + " | ".join(str(a) for a in above) + f" | {highest:.0f}")

        per_station = {}
        print(f"\n2. the {args.top} stations with the highest reading: station | n | p99 | p99.9 | max | "
              "n above the first level | first..last time above it")
        for pollutant in Pollutant:
            first_level = LEVELS[pollutant][0]
            rows = session.execute(
                select(RawSensorReading.station_id, func.count(),
                       func.percentile_cont(0.99).within_group(value),
                       func.percentile_cont(0.999).within_group(value),
                       func.max(value), func.count().filter(value > first_level),
                       func.min(RawSensorReading.observed_at).filter(value > first_level),
                       func.max(RawSensorReading.observed_at).filter(value > first_level))
                .where(in_window, RawSensorReading.pollutant == pollutant)
                .group_by(RawSensorReading.station_id)
                .order_by(func.max(value).desc())
            ).all()
            for sid, n, p99, p999, highest, n_above, _first, _last in rows:
                per_station[(sid, pollutant)] = (n, p99, p999, highest, n_above)
            print(f"  {pollutant.value} (first level {first_level}); "
                  f"{sum(1 for r in rows if r[5])} of {len(rows)} stations have a reading above it")
            for sid, n, p99, p999, highest, n_above, first, last in rows[:args.top]:
                print(f"    {sid} {names.get(sid, '?')[:34]} | {n} | {p99:.0f} | {p999:.0f} | {highest:.0f} | "
                      f"{n_above} | {_t(first)}..{_t(last)}")

        fresh = now - timedelta(hours=MAX_INPUT_STALENESS_HOURS)
        recent = (Forecast.forecast_made_at >= fresh, Forecast.target_time >= fresh)
        newest = (
            select(Forecast.station_id, Forecast.pollutant, func.max(Forecast.forecast_made_at).label("made_at"))
            .where(*recent).group_by(Forecast.station_id, Forecast.pollutant).subquery()
        )
        forecasts = session.execute(
            select(Forecast)
            .join(newest, (Forecast.station_id == newest.c.station_id) & (Forecast.pollutant == newest.c.pollutant)
                  & (Forecast.forecast_made_at == newest.c.made_at))
            .where(*recent)
            .order_by(Forecast.quantile_high.desc())
        ).scalars().all()
        wild = [f for f in forecasts if f.quantile_high > LEVELS[f.pollutant][0]]
        print(f"\n3. current forecasts above the first level: {len(wild)} of {len(forecasts)} rows, "
              f"{len({(f.station_id, f.pollutant) for f in wild})} station/pollutant pairs")
        print("   station | pollutant horizon | low / point / high | that station's readings: p99.9 / max / n above the first level")
        for f in wild:
            _n, _p99, p999, highest, n_above = per_station.get((f.station_id, f.pollutant), (0, 0, 0, 0, 0))
            print(f"    {f.station_id} {names.get(f.station_id, '?')[:34]} | {f.pollutant.value} {f.horizon_hours}h | "
                  f"{f.quantile_low:.0f} / {f.point_forecast:.0f} / {f.quantile_high:.0f} | "
                  f"{p999:.0f} / {highest:.0f} / {n_above}")
        clean = {(f.station_id, f.pollutant) for f in wild if per_station.get((f.station_id, f.pollutant), (0,) * 5)[4] == 0}
        print(f"\n   pairs with a wild forecast and NO reading above the first level: {len(clean)}"
              + (f" {sorted((sid, pol.value) for sid, pol in clean)}" if clean else "")
              + "  (0 = the readings explain every wild forecast)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
