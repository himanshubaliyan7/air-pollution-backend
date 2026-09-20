import streamlit as st

from dashboard.client import get_exceedance_summary

st.set_page_config(page_title="Go / No-Go", page_icon="🚦", layout="wide")
st.title("Outdoor Practice: Go / No-Go")

station_id = st.session_state.get("station_id")
pollutant = st.session_state.get("pollutant", "pm25")

if not station_id:
    st.info("Pick a station on the main page first.")
    st.stop()

try:
    summary = get_exceedance_summary(station_id, pollutant=pollutant, days_ahead=5)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load forecast: {exc}")
    st.stop()

if not summary["days"]:
    st.warning(
    "No current forecast for this station - do NOT treat this as a Go. Sensor data for many stations "
    "is delayed upstream, so a forecast is only produced when the latest reading is recent."
)
    st.stop()

BANNER = {
    "go": ("🟢", "Go", "#1a7f37"),
    "caution": ("🟡", "Caution", "#9a6700"),
    "no-go": ("🔴", "No-Go", "#cf222e"),
}
emoji, label, color = BANNER[summary["overall_recommendation"]]
st.markdown(
    f"<div style='padding:1.5rem;border-radius:0.5rem;background:{color}22;border:2px solid {color}'>"
    f"<h2 style='margin:0;color:{color}'>{emoji} Overall: {label}</h2></div>",
    unsafe_allow_html=True,
)

st.divider()
st.subheader("Next 5 days")

cols = st.columns(len(summary["days"]))
for col, day in zip(cols, summary["days"]):
    day_emoji, day_label, day_color = ("🔴", "No-Go", "#cf222e") if day["exceedance_flag"] else (
        ("🟡", "Caution", "#9a6700") if day["exceedance_probability"] >= 0.15 else ("🟢", "Go", "#1a7f37")
    )
    with col:
        st.markdown(
            f"<div style='text-align:center;padding:1rem;border-radius:0.5rem;"
            f"background:{day_color}22;border:1px solid {day_color}'>"
            f"<div style='font-size:1.5rem'>{day_emoji}</div>"
            f"<b>{day['date']}</b><br>{day_label}<br>"
            f"<small>{day['aqi_category'].replace('_', ' ').title()}</small><br>"
            f"<small>{day['exceedance_probability']*100:.0f}% chance</small>"
            f"</div>",
            unsafe_allow_html=True,
        )
