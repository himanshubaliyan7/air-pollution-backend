"""Turning CPCB AQI sub-indices into categories and an overall AQI.

Works on a region's thresholds file (config/thresholds_*.yaml), whose
`aqi_range` bands map an AQI value to a category (e.g. 201-300 = poor).
"""

# Must be shown wherever CPCB AQI data is displayed (GODL-India licence), whichever feed it came from.
# Lives here, not in ingestion/, so the API image (which has no `requests`) can import it.
ATTRIBUTION = (
    "Source: Central Pollution Control Board (CPCB), Ministry of Environment, Forest and Climate Change, "
    "Government of India, published on data.gov.in under the Government Open Data License - India (GODL-India)."
)

# CPCB: the overall AQI is the worst sub-index, and is only valid when at least
# three pollutants are reported, one of which is PM2.5 or PM10.
MIN_POLLUTANTS_FOR_OVERALL = 3
PARTICULATE_POLLUTANTS = {"PM2.5", "PM10"}


def category_for_sub_index(thresholds: dict, value: float) -> str:
    """Category whose aqi_range contains `value` (the last category above the top)."""
    bands = next(iter(thresholds["pollutants"].values()))["breakpoints"]
    for band in bands:
        if value <= band["aqi_range"][1]:
            return band["category"]
    return bands[-1]["category"]


def concentration_from_sub_index(thresholds: dict, pollutant: str, sub_index: float) -> tuple[float, bool]:
    """(concentration, capped) for a CPCB sub-index, linear within each band
    (the inverse of CPCB's own formula). `capped` is True at the top of the
    scale, where every higher concentration reads the same: the value returned
    is then the band's upper bound, a floor rather than the real reading."""
    bands = thresholds["pollutants"][pollutant]["breakpoints"]
    top_index, top_conc = bands[-1]["aqi_range"][1], float(bands[-1]["conc_range"][1])
    if sub_index >= top_index:
        return top_conc, True
    for band in bands:
        (i_lo, i_hi), (c_lo, c_hi) = band["aqi_range"], band["conc_range"]
        if sub_index <= i_hi:
            return c_lo + (max(sub_index, i_lo) - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), False
    return top_conc, True


def at_or_above_health_threshold(thresholds: dict, category: str) -> bool:
    """True when `category` is at or worse than the region's declared
    health-threshold category (the level at which outdoor practice is not
    recommended). Decided here so no client has to know the category order."""
    order = [b["category"] for b in next(iter(thresholds["pollutants"].values()))["breakpoints"]]
    return order.index(category) >= order.index(thresholds["health_threshold_category"])


def overall_aqi(sub_indices: dict[str, float]) -> tuple[int, str] | None:
    """(worst sub-index rounded, the pollutant driving it), or None when CPCB's
    minimum-pollutants rule is not met."""
    usable = {p: v for p, v in sub_indices.items() if v is not None}
    if len(usable) < MIN_POLLUTANTS_FOR_OVERALL or not (PARTICULATE_POLLUTANTS & usable.keys()):
        return None
    driver = max(usable, key=usable.get)
    return round(usable[driver]), driver
