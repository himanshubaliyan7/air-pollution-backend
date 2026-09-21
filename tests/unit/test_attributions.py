from fastapi.testclient import TestClient

from api.main import app
from common.aqi import ATTRIBUTION
from common.attributions import get_attributions


def test_required_credits_cover_every_licence_that_demands_one():
    by_id = {a.id: a for a in get_attributions(year=2026)}
    assert by_id["cpcb-data-gov-in"].text == ATTRIBUTION and by_id["cpcb-data-gov-in"].required
    assert "Open-Meteo.com" in by_id["open-meteo"].text and "CC BY 4.0" in by_id["open-meteo"].text
    assert by_id["open-meteo"].required
    assert by_id["copernicus-era5"].text == "Generated using Copernicus Climate Change Service information 2026."
    assert by_id["copernicus-era5"].required
    assert by_id["openaq"].required is False  # courtesy only: we have not verified a licence duty


def test_endpoint_serves_the_credits_with_urls():
    body = TestClient(app).get("/api/v1/attributions").json()
    assert {a["id"] for a in body} >= {"cpcb-data-gov-in", "open-meteo", "copernicus-era5"}
    assert all(a["text"] and a["applies_to"] for a in body)
    assert all(a["url"].startswith("https://") for a in body)
