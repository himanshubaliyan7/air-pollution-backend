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


def test_sub_index_from_concentration_follows_the_cpcb_bands():
    from common.aqi import category_for_sub_index, concentration_from_sub_index, sub_index_from_concentration
    from common.constants import Pollutant
    from models.exceedance import get_aqi_category, load_thresholds

    thresholds = load_thresholds()
    # CPCB's published anchor points and band edges for PM2.5.
    for conc, index in [(0, 0), (30, 50), (31, 51), (60, 100), (61, 101), (90, 200), (91, 201), (120, 300),
                        (121, 301), (250, 400), (251, 401), (380, 500)]:
        assert sub_index_from_concentration(thresholds, "pm25", conc) == index, conc
    assert sub_index_from_concentration(thresholds, "pm25", 62) == 104  # 101 + 1 * 99 / 29
    assert sub_index_from_concentration(thresholds, "no2", 181) == 201

    # Above the scale and below zero it stays on the scale.
    assert sub_index_from_concentration(thresholds, "pm25", 5000) == 500
    assert sub_index_from_concentration(thresholds, "pm25", -3) == 0

    # The index always lands in the category the concentration is graded as,
    # including values between CPCB's integer ranges, and it inverts the inverse.
    for conc in [0.4, 30.5, 45, 60.5, 75.2, 90.9, 91, 120.7, 200, 250.5, 379]:
        index = sub_index_from_concentration(thresholds, "pm25", conc)
        assert category_for_sub_index(thresholds, index) == get_aqi_category(thresholds, Pollutant.PM25, conc), conc
    for index in [10, 75, 150, 250, 350, 450]:
        conc, _ = concentration_from_sub_index(thresholds, "pm25", index)
        assert sub_index_from_concentration(thresholds, "pm25", conc) == index
