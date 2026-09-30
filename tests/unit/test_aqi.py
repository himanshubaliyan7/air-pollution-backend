from common.aqi import at_or_above_health_threshold, category_for_sub_index, overall_aqi
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


def test_health_threshold_flag_uses_the_regions_declared_category():
    thresholds = get_region("delhi-ncr").thresholds()  # health_threshold_category: poor
    flags = {c: at_or_above_health_threshold(thresholds, c) for c in
             ["good", "satisfactory", "moderate", "poor", "very_poor", "severe"]}
    assert flags == {"good": False, "satisfactory": False, "moderate": False, "poor": True, "very_poor": True, "severe": True}


def test_sub_index_inverts_to_concentration_at_cpcb_anchor_points():
    from common.aqi import concentration_from_sub_index

    thresholds = get_region("delhi-ncr").thresholds()
    pm25 = {0: 0.0, 50: 30.0, 51: 31.0, 100: 60.0, 200: 90.0, 201: 91.0, 300: 120.0, 400: 250.0, 401: 251.0}
    for sub_index, conc in pm25.items():
        assert concentration_from_sub_index(thresholds, "pm25", sub_index) == (conc, False), sub_index
    value, capped = concentration_from_sub_index(thresholds, "pm25", 150)  # band 101-200 <-> 61-90, linear
    assert (round(value, 2), capped) == (75.35, False)
    assert concentration_from_sub_index(thresholds, "no2", 100) == (80.0, False)


def test_sub_index_at_the_top_of_the_scale_is_a_capped_floor():
    from common.aqi import concentration_from_sub_index

    thresholds = get_region("delhi-ncr").thresholds()
    assert concentration_from_sub_index(thresholds, "pm25", 500) == (380.0, True)
    assert concentration_from_sub_index(thresholds, "pm25", 612) == (380.0, True)
