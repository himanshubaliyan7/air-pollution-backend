"""Read-only: last week's day-ahead recall per region and pollutant, counted
two ways - every stored daily-mean row (what evaluation_monitoring_dag counted
before the fix) and only the verdict shown the evening before (after it).

    sudo docker exec -i docker-airflow-scheduler-1 python - [--days 7] < scripts/daily_drift_recount.py
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from common.constants import ForecastTarget
from db.models import Forecast
from db.session import get_session
from models import exceedance
from orchestration.plugins.common.regional import station_region_name, thresholds_for_station
from orchestration.plugins.common.tasks import _active_stations, _actuals_for_evaluation, _verdicts_shown

DAILY = ForecastTarget.DAILY_MEAN


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days + 1)
    # (region, pollutant, way) -> [days above threshold, of which flagged]
    counts = defaultdict(lambda: [0, 0])
    cache: dict = {}
    session = get_session()
    try:
        for station in _active_stations(session):
            region = station_region_name(station.lat, station.lon)
            thresholds = thresholds_for_station(station, cache)
            rows = session.execute(
                select(Forecast).where(
                    Forecast.station_id == station.station_id, Forecast.target == DAILY.value,
                    Forecast.horizon_hours == 24, Forecast.target_time >= start, Forecast.target_time <= now - timedelta(days=1),
                )
            ).scalars().all()
            by_pollutant = defaultdict(list)
            for r in rows:
                by_pollutant[r.pollutant].append(r)
            for pollutant, prows in by_pollutant.items():
                threshold = exceedance.get_health_threshold_concentration(pollutant, thresholds)
                actual = _actuals_for_evaluation(session, station, pollutant, start, now, DAILY)
                for way, chosen in (("every row", prows), ("shown", _verdicts_shown(prows))):
                    for r in chosen:
                        value = actual.get(r.target_time)
                        if value is not None and value > threshold:
                            c = counts[(region, pollutant.value, way)]
                            c[0] += 1
                            c[1] += int(r.exceedance_flag)
    finally:
        session.close()
    print(f"last {args.days} days to {now:%Y-%m-%d %H:%M} UTC; horizon 24 h")
    for (region, pollutant, way), (above, flagged) in sorted(counts.items()):
        recall = f"{flagged / above:.0%}" if above else "-"
        print(f"{region:12} {pollutant:6} {way:10} above threshold {above:5}  flagged {flagged:5}  recall {recall}")


if __name__ == "__main__":
    main()
