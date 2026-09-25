"""Concentration units. Thresholds (config/thresholds_cpcb.yaml), models and the
API all work in ug/m3, but OpenAQ publishes Delhi NO2 in ppb - stored as-is
until 2026-09-25, which understated NO2 by ~1.9x against the ug/m3 threshold.
Every reading is converted here, at the ingestion boundary.
"""

import logging

from common.constants import Pollutant

logger = logging.getLogger(__name__)

CANONICAL_UNIT = "µg/m³"  # the spelling OpenAQ uses, already on every stored PM2.5 row
_MASS_UNITS = {"µg/m³", "ug/m3", "μg/m³"}  # includes the Greek-mu spelling
# ug/m3 per ppb at 25 C and 1 atm (molar mass / 24.45 L/mol), the reference
# conditions of India's NAAQS.
PPB_TO_UG_M3 = {Pollutant.NO2: 46.0055 / 24.45}


def to_canonical(pollutant: Pollutant, value: float, unit: str) -> tuple[float, str] | None:
    """(value, CANONICAL_UNIT), or None for a unit we cannot convert - better a
    missing hour than a silently wrong one."""
    unit = (unit or "").strip()
    if unit in _MASS_UNITS:
        return value, CANONICAL_UNIT
    if unit == "ppb" and pollutant in PPB_TO_UG_M3:
        return value * PPB_TO_UG_M3[pollutant], CANONICAL_UNIT
    logger.warning("Dropping %s reading with unconvertible unit %r", pollutant.value, unit)
    return None
