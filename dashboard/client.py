"""Thin HTTP client against the API - the dashboard never touches the DB
directly, so it can be redeployed/replaced independently of the API."""

import os

import requests

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000")


def _get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{API_BASE_URL}/api/v1{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def list_stations() -> list[dict]:
    return _get("/stations")


def get_forecast(station_id: str, pollutant: str = "pm25") -> dict:
    return _get(f"/forecast/{station_id}", params={"pollutant": pollutant})


def get_exceedance_summary(station_id: str, pollutant: str = "pm25", days_ahead: int = 5) -> dict:
    return _get(f"/forecast/{station_id}/exceedance", params={"pollutant": pollutant, "days_ahead": days_ahead})


def get_history(station_id: str, pollutant: str = "pm25", lookback_days: int = 14) -> dict:
    return _get(f"/forecast/{station_id}/history", params={"pollutant": pollutant, "lookback_days": lookback_days})


def get_model_health(station_id: str | None = None, pollutant: str | None = None) -> list[dict]:
    params = {}
    if station_id:
        params["station_id"] = station_id
    if pollutant:
        params["pollutant"] = pollutant
    return _get("/model-health", params=params)
