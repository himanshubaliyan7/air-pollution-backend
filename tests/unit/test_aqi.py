from common.aqi import category_for_sub_index, overall_aqi
from common.regions import get_region


def test_category_follows_cpcb_aqi_bands():
    thresholds = get_region("delhi-ncr").thresholds()
    cases = {0: "good", 50: "good", 51: "satisfactory", 100: "satisfactory", 101: "moderate", 200: "moderate",
             201: "poor", 300: "poor", 301: "very_poor", 400: "very_poor", 401: "severe", 500: "severe", 731: "severe"}
    for value, expected in cases.items():
        assert category_for_sub_index(thresholds, value) == expected, value


def test_overall_is_the_worst_sub_index_and_names_its_pollutant():
    assert overall_aqi({"PM2.5": 166.0, "PM10": 140.0, "NO2": 27.0}) == (166, "PM2.5")
    assert overall_aqi({"PM2.5": 44.0, "PM10": 92.0, "SO2": 12.0, "CO": 80.0}) == (92, "PM10")


def test_overall_requires_three_pollutants_including_a_particulate():
    """CPCB: at least three pollutants reported, one of them PM2.5 or PM10."""
    assert overall_aqi({"PM2.5": 166.0, "NO2": 27.0}) is None
    assert overall_aqi({"NO2": 27.0, "SO2": 12.0, "CO": 80.0}) is None
    assert overall_aqi({"PM2.5": 166.0, "NO2": None, "SO2": None}) is None  # NA does not count as reported
