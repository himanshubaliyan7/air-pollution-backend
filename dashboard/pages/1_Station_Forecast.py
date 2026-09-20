import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.client import get_forecast
from models.exceedance import get_health_threshold_concentration, load_thresholds
from common.constants import Pollutant

st.set_page_config(page_title="Station Forecast", page_icon="📈", layout="wide")
st.title("Station Forecast")

station_id = st.session_state.get("station_id")
pollutant = st.session_state.get("pollutant", "pm25")

if not station_id:
    st.info("Pick a station on the main page first.")
    st.stop()

try:
    series = get_forecast(station_id, pollutant=pollutant)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load forecast: {exc}")
    st.stop()

if not series["forecasts"]:
    st.warning("No forecast available yet for this station.")
    st.stop()

df = pd.DataFrame(series["forecasts"])
df["target_time"] = pd.to_datetime(df["target_time"])

fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=pd.concat([df["target_time"], df["target_time"][::-1]]),
        y=pd.concat([df["quantile_high"], df["quantile_low"][::-1]]),
        fill="toself",
        fillcolor="rgba(66,133,244,0.15)",
        line=dict(color="rgba(255,255,255,0)"),
        name="10th-90th percentile",
        hoverinfo="skip",
    )
)
fig.add_trace(
    go.Scatter(x=df["target_time"], y=df["point_forecast"], mode="lines+markers", name="Median forecast", line=dict(color="#4285f4"))
)

threshold = get_health_threshold_concentration(Pollutant(pollutant), load_thresholds())
fig.add_hline(y=threshold, line_dash="dash", line_color="#cf222e", annotation_text="Health threshold")

fig.update_layout(
    xaxis_title="Time",
    yaxis_title=f"{pollutant.upper()} (ug/m3)",
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
)
st.plotly_chart(fig, use_container_width=True)

with st.expander("Raw forecast values"):
    st.dataframe(df, use_container_width=True)
