"""Run the forecast step once, outside forecast_dag.

With --target it writes forecasts for the model family that is NOT being
served yet. Nothing reads those rows until config/settings.yaml
forecast.target is switched, so a switch can go live with forecasts already in
place instead of showing "no data" until the next hourly run. Train that
family first (scripts/retrain_models.py --apply --target ...).

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --target daily_mean < scripts/generate_forecasts.py
    ... python - --target daily_mean --region mumbai < scripts/generate_forecasts.py

--region limits the run to the stations of one region (default: all).
"""

import argparse

from common.constants import ForecastTarget
from common.logging_conf import configure_logging
from common.regions import get_region, region_for_point
from orchestration.plugins.common import tasks
from orchestration.plugins.common.tasks import generate_forecasts


def limit_to_region(region_id: str) -> None:
    """generate_forecasts takes no station filter: narrow the stations it reads
    for this process only."""
    all_active = tasks._active_stations
    tasks._active_stations = lambda session: [
        s for s in all_active(session) if (r := region_for_point(s.lat, s.lon)) is not None and r.id == region_id
    ]


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=[t.value for t in ForecastTarget], help="default: the family being served")
    parser.add_argument("--region", help="region id (config/regions.yaml); default: all regions")
    args = parser.parse_args()
    if args.region:
        if get_region(args.region) is None:
            parser.error(f"unknown region {args.region!r}")
        limit_to_region(args.region)
    result = generate_forecasts(ForecastTarget(args.target) if args.target else None)
    print(f"forecasts written: {result['forecasts_written']}; "
          f"station/pollutant pairs skipped for stale input: {result['skipped_stale']}")


if __name__ == "__main__":
    main()
