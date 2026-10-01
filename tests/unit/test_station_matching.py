"""CPCB feed station -> our station (ingestion/loaders/aqi_snapshot_loader.py).

The cases are the real ones found on 2026-10-01: CPCB data for Anand Vihar and
ITO went to a dead duplicate entry without models, and six CPCB stations
matched nothing because OpenAQ places them kilometres away."""

from datetime import datetime, timedelta, timezone

from common.constants import SensorSourceName
from db.models import Station
from ingestion.loaders.aqi_snapshot_loader import match_station, site_and_operator

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _station(sid, name, lat, lon):
    return Station(
        station_id=sid, name=name, lat=lat, lon=lon, city="Delhi", state="Delhi",
        source=SensorSourceName.OPENAQ, source_location_id=sid, is_active=True, created_at=NOW,
    )


def _match(stations, name, lat, lon, newest=None):
    station = match_station(stations, name, lat, lon, newest)
    return station.station_id if station else None


def test_site_and_operator_ignores_the_city_and_knows_the_imd_rename():
    assert site_and_operator("Pusa, Delhi - DPCC") == ("pusa", "dpcc")
    assert site_and_operator("Aya Nagar, New Delhi - IMD") == site_and_operator("Aya Nagar, Delhi - IITM")
    assert site_and_operator("North Campus, DU, Delhi - IITM") == ("north campus", "imd")
    assert site_and_operator("Knowledge Park - III, Greater Noida - UPPCB") == ("knowledge park iii", "uppcb")
    assert site_and_operator("Sector-1, Noida - UPPCB") == ("sector 1", "uppcb")
    assert site_and_operator("Sector 1, Noida extension") is None  # a private sensor: no operator
    assert site_and_operator("Air Check") is None


def test_a_single_station_within_the_radius_matches_whatever_its_name():
    stations = [_station("a", "Anand Vihar", 28.6469, 77.3158), _station("far", "Far", 28.70, 77.40)]
    assert _match(stations, "Something Else - DPCC", 28.6470, 77.3159) == "a"
    assert _match(stations, "Something Else - DPCC", 28.80, 77.10) is None


def test_of_two_entries_for_one_site_the_one_with_openaq_history_wins():
    # The feed's coordinates are exactly the dead entry's; the live one is 90 m away.
    dead = _station("openaq:5509", "Anand Vihar, Delhi - DPCC", 28.647622, 77.315809)
    live = _station("openaq:235", "Anand Vihar, New Delhi - DPCC", 28.6468, 77.3160)
    newest = {"openaq:235": NOW - timedelta(hours=47)}
    for stations in ([dead, live], [live, dead]):
        assert _match(stations, "Anand Vihar, Delhi - DPCC", 28.647622, 77.315809, newest) == "openaq:235"
    # Without any OpenAQ history to tell them apart, the nearest wins.
    assert _match([live, dead], "Anand Vihar, Delhi - DPCC", 28.647622, 77.315809) == "openaq:5509"
    # Both have history: the one still reporting wins.
    newest["openaq:5509"] = NOW - timedelta(days=400)
    assert _match([dead, live], "Anand Vihar, Delhi - DPCC", 28.647622, 77.315809, newest) == "openaq:235"


def test_a_misplaced_station_matches_by_site_and_operator():
    # OpenAQ puts both Pusa stations on one point, 2.6 km from CPCB's coordinates.
    stations = [
        _station("openaq:6356", "Pusa, Delhi - DPCC", 28.6396, 77.1463),
        _station("openaq:5404", "Pusa, Delhi - IMD", 28.6396, 77.1463),
        _station("openaq:6254594", "Talkatora Garden, Delhi - DPCC", 28.6225, 77.1900),
    ]
    assert _match(stations, "Pusa, Delhi - DPCC", 28.6379, 77.1731) == "openaq:6356"
    assert _match(stations, "Pusa, Delhi - IITM", 28.6377, 77.1728) == "openaq:5404"
    assert _match(stations, "Shadipur, Delhi - CPCB", 28.6379, 77.1731) is None  # no such station of ours


def test_two_operators_on_one_site_each_get_their_own_feed_station():
    # After their coordinates are corrected the two Pusa stations stand 83 m apart,
    # so both are within the radius of both feed stations.
    dpcc = _station("openaq:6356", "Pusa, Delhi - DPCC", 28.636818, 77.173597)
    imd = _station("openaq:5404", "Pusa, Delhi - IMD", 28.63611, 77.173332)
    newest = {"openaq:6356": NOW - timedelta(hours=1), "openaq:5404": NOW - timedelta(hours=47)}
    for stations in ([dpcc, imd], [imd, dpcc]):
        assert _match(stations, "Pusa, Delhi - DPCC", 28.636818, 77.173597, newest) == "openaq:6356"
        # The other station reports more recently, but it is not this feed station.
        assert _match(stations, "Pusa, Delhi - IITM", 28.63611, 77.173332, newest) == "openaq:5404"
    # A feed name that fits neither still takes the usual rule.
    assert _match([dpcc, imd], "Somewhere Else - CPCB", 28.6365, 77.1734, newest) == "openaq:6356"


def test_a_name_match_needs_the_operator_and_a_plausible_distance():
    private = _station("openaq:6105800", "Sector 1, Noida extension", 28.59, 77.44)
    assert _match([private], "Sector-1, Noida - UPPCB", 28.5898, 77.3101) is None
    north_campus = _station("openaq:5610", "North Campus, DU, Delhi - IMD", 28.6573814, 77.1585447)  # 6.5 km off
    assert _match([north_campus], "North Campus, DU, Delhi - IITM", 28.689924, 77.214261) == "openaq:5610"
    rohtak = _station("openaq:7283", "MD University, Rohtak - HSPCB", 28.87793, 76.620825)  # 46 km off
    assert _match([rohtak], "MD University, Rohtak - HSPCB", 28.52123, 76.37138) is None
