import pandas as pd
import streamlit as st

from dashboard.client import get_history, get_model_health

st.set_page_config(page_title="Model Health", page_icon="🩺", layout="wide")
st.title("Model Health (operators)")

station_id = st.session_state.get("station_id")
pollutant = st.session_state.get("pollutant", "pm25")

if not station_id:
    st.info("Pick a station on the main page first.")
    st.stop()

try:
    health_rows = get_model_health(station_id=station_id, pollutant=pollutant)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load model health: {exc}")
    health_rows = []

if not health_rows:
    st.warning("No evaluation history yet - the evaluation_monitoring_dag needs at least one run.")
else:
    df = pd.DataFrame(health_rows).sort_values("evaluation_window_end")
    st.subheader("Exceedance-day classification (primary metric)")
    st.line_chart(df.set_index("evaluation_window_end")[["precision", "recall", "f1"]])
    st.subheader("Point-forecast error (secondary)")
    st.line_chart(df.set_index("evaluation_window_end")[["mae", "rmse"]])
    st.dataframe(df, use_container_width=True)

st.divider()
st.subheader("Realized vs. forecast")
try:
    history = get_history(station_id, pollutant=pollutant, lookback_days=14)
    hdf = pd.DataFrame(history["points"])
    if not hdf.empty:
        hdf["time"] = pd.to_datetime(hdf["time"])
        st.line_chart(hdf.set_index("time")[["actual", "forecast_value"]])
    else:
        st.info("No realized/forecast history in the last 14 days yet.")
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load history: {exc}")
