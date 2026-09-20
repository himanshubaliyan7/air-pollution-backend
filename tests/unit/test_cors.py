"""A browser frontend on another origin needs CORS headers, including for the
preflight that precedes its JSON POST (subscriptions)."""

from fastapi.testclient import TestClient

from api.main import app

ORIGIN = "https://example-frontend.lovable.app"


def test_preflight_for_subscription_post_is_allowed():
    resp = TestClient(app).options(
        "/api/v1/subscriptions",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] in ("*", ORIGIN)
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_simple_get_carries_cors_header():
    resp = TestClient(app).get("/openapi.json", headers={"Origin": ORIGIN})
    assert resp.headers["access-control-allow-origin"] in ("*", ORIGIN)
