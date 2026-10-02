"""Run the forecast step once, outside forecast_dag.

With --target it writes forecasts for the model family that is NOT being
served yet. Nothing reads those rows until config/settings.yaml
forecast.target is switched, so a switch can go live with forecasts already in
place instead of showing "no data" until the next hourly run. Train that
family first (scripts/retrain_models.py --apply --target ...).

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --target daily_mean < scripts/generate_forecasts.py
"""

import argparse

from common.constants import ForecastTarget
from common.logging_conf import configure_logging
from orchestration.plugins.common.tasks import generate_forecasts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=[t.value for t in ForecastTarget], help="default: the family being served")
    args = parser.parse_args()
    result = generate_forecasts(ForecastTarget(args.target) if args.target else None)
    print(f"forecasts written: {result['forecasts_written']}; "
          f"station/pollutant pairs skipped for stale input: {result['skipped_stale']}")


if __name__ == "__main__":
    main()
