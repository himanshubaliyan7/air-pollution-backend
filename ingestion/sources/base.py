"""Pluggable sensor-source interface.

Every sensor data provider (OpenAQ for Delhi NCR today; a future AirNow
implementation for a US region) implements SensorSource so ingestion_dag and
the loaders never need to know which provider they're talking to. Adding a
new source means writing one new class here - no other module changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from common.constants import Pollutant, SensorSourceName


@dataclass(frozen=True)
class StationMetadata:
    source_location_id: str
    name: str
    lat: float
    lon: float
    city: str
    state: str


@dataclass(frozen=True)
class SensorReading:
    """Normalized reading DTO - every source adapter must produce this shape
    regardless of the provider's native response format."""

    source_location_id: str
    pollutant: Pollutant
    value: float
    unit: str
    observed_at: datetime  # timezone-aware, UTC
    source_record_id: str | None = None


class SensorSource(ABC):
    """Abstract interface every sensor data provider implements."""

    @property
    @abstractmethod
    def source_name(self) -> SensorSourceName: ...

    @abstractmethod
    def list_stations(self, *, bbox: tuple[float, float, float, float], country: str) -> list[StationMetadata]:
        """Discover stations within a bounding box (min_lon, min_lat, max_lon, max_lat).

        Called rarely (station bootstrap / periodic re-sync), never on every
        hourly ingestion run, to stay within provider rate limits.
        """
        ...

    @abstractmethod
    def fetch_readings(
        self,
        *,
        source_location_ids: list[str],
        pollutants: list[Pollutant],
        start: datetime,
        end: datetime,
    ) -> list[SensorReading]:
        """Fetch readings for the given stations/pollutants in [start, end) UTC."""
        ...
