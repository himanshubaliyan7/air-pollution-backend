"""Credits every screen of every client must show, because the licences behind our data
require it (see docs/api_compliance.md). Served by GET /api/v1/attributions so a frontend
never hardcodes them. Kept free of third-party imports so the API image can load it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from common.aqi import ATTRIBUTION as CPCB_ATTRIBUTION


@dataclass(frozen=True)
class Attribution:
    id: str
    text: str
    url: str | None
    required: bool  # True: the licence requires showing it. False: a courtesy credit.
    applies_to: str  # what it covers, in plain words


def get_attributions(year: int | None = None) -> list[Attribution]:
    year = year or datetime.now(timezone.utc).year
    return [
        Attribution(
            id="cpcb-data-gov-in",
            text=CPCB_ATTRIBUTION,
            url="https://www.data.gov.in/",
            required=True,
            applies_to="Current air-quality readings (AQI) shown for each station",
        ),
        Attribution(
            id="open-meteo",
            text="Weather data by Open-Meteo.com (CC BY 4.0).",
            url="https://open-meteo.com/",
            required=True,
            applies_to="Near-term weather used by the forecasts",
        ),
        Attribution(
            id="copernicus-era5",
            text=f"Generated using Copernicus Climate Change Service information {year}.",
            url="https://climate.copernicus.eu/",
            required=True,
            applies_to="Historical weather (ERA5) used to build the forecasts",
        ),
        Attribution(
            id="openaq",
            text="Historical air-quality measurements from OpenAQ (openaq.org) and its data providers.",
            url="https://openaq.org/",
            required=False,
            applies_to="Historical measurements used to train the forecast models",
        ),
    ]
