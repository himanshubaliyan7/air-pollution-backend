import streamlit as st

from dashboard.client import list_stations

st.set_page_config(page_title="Delhi NCR Air Quality", page_icon="🌫️", layout="wide")

st.title("Delhi NCR Air Quality Forecast")
st.caption("Hourly PM2.5 / NO2 forecasts for school outdoor-practice decisions")

try:
    stations = list_stations()
except Exception as exc:  # noqa: BLE001 - surfaced directly to the operator, not swallowed
    st.error(f"Could not reach the API: {exc}")
    stations = []

if not stations:
    st.warning(
        "No stations found yet. Run `python -m scripts.seed_stations` (needs OPENAQ_API_KEY) "
        "to discover Delhi NCR stations, then let ingestion/feature/forecast DAGs run at least once."
    )
else:
    st.session_state.setdefault("station_id", stations[0]["station_id"])
    station_names = {s["station_id"]: f"{s['name']} ({s['station_id']})" for s in stations}
    selected = st.selectbox(
        "Station",
        options=list(station_names.keys()),
        format_func=lambda sid: station_names[sid],
        key="station_id",
    )
    st.selectbox("Pollutant", options=["pm25", "no2"], key="pollutant")

    st.page_link("pages/2_Go_NoGo.py", label="Go to the Go / No-Go view", icon="🚦")
    st.page_link("pages/1_Station_Forecast.py", label="View the detailed forecast chart", icon="📈")
    st.page_link("pages/3_Model_Health.py", label="View model health (operators)", icon="🩺")
