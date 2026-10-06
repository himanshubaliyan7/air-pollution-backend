"""CPCB AQI sub-index helpers shared by the owner-run scripts (which are piped into
the container, where the scripts package is not importable)."""

from common.constants import Pollutant

FEED_IDS = {"PM2.5": Pollutant.PM25, "NO2": Pollutant.NO2}


def inverter(breakpoints: list[dict]):
    """Sub-index -> concentration, linear within each CPCB band. Returns
    (value, capped): capped is True at the top of the scale, where higher
    concentrations are indistinguishable."""
    top = breakpoints[-1]["aqi_range"][1]

    def invert(sub_index: float) -> tuple[float, bool]:
        if sub_index >= top:
            return float(breakpoints[-1]["conc_range"][1]), True
        for bp in breakpoints:
            i_lo, i_hi = bp["aqi_range"]
            c_lo, c_hi = bp["conc_range"]
            if sub_index <= i_hi:
                i = max(sub_index, i_lo)
                return c_lo + (i - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), False
        return float(breakpoints[-1]["conc_range"][1]), True

    return invert, top
