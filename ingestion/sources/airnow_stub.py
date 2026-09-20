"""Not implemented.

EPA AirNow (https://docs.airnowapi.org) only covers US monitoring stations,
so it isn't wired up for the Delhi NCR region this system currently targets.
This file exists to confirm the SensorSource interface (ingestion/sources/base.py)
needs no redesign to add a US region later: a future AirNowSource would
implement the same three members as OpenAQSource does in ingestion/sources/openaq.py:

    class AirNowSource(SensorSource):
        source_name -> SensorSourceName.AIRNOW

        def list_stations(self, *, bbox, country) -> list[StationMetadata]:
            # GET https://www.airnowapi.org/aq/data/ or the AirNow monitoring
            # site list endpoint, filtered to the bbox/country, mapped into
            # StationMetadata the same way OpenAQSource.list_stations does.
            ...

        def fetch_readings(self, *, source_location_ids, pollutants, start, end) -> list[SensorReading]:
            # GET https://www.airnowapi.org/aq/data/ with parameters=PM25,NO2
            # and the requested monitoring site ids / date range, mapped into
            # SensorReading DTOs.
            ...

Once implemented, it would be registered in ingestion/config.py's
SENSOR_SOURCE_REGISTRY alongside "openaq" and selected per-region there -
no changes needed to loaders, the ingestion DAG, or downstream feature/model
code, since everything downstream already only depends on the normalized
StationMetadata / SensorReading DTOs.
"""
