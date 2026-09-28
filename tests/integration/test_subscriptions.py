"""Phase 4: owner-verified subscriptions, the daily digest and retention.

No test can reach a real SMTP server: `outbox` swaps smtplib.SMTP for an
in-memory fake and points the settings at a fake host, so the mailer runs its
real code path.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from common.config import get_settings
from common.constants import AlertStatus, Pollutant
from db.models import AlertSubscription, DigestLog
from tests.integration.forecast_helpers import add_station, seed_forecast_run

UTC = timezone.utc
# 12:30 UTC = 18:00 IST, when alert_digest_dag runs; "tomorrow" is 2026-09-26 in Delhi.
DIGEST_NOW = datetime(2026, 9, 25, 12, 30, tzinfo=UTC)


class FakeSMTP:
    outbox: list = []
    fail = False

    def __init__(self, host, port=0, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg, *args, **kwargs):
        if FakeSMTP.fail:
            raise ConnectionError("fake SMTP failure")
        FakeSMTP.outbox.append(msg)
        return {}


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    FakeSMTP.outbox, FakeSMTP.fail = [], False
    monkeypatch.setattr("alerting.mailer.smtplib.SMTP", FakeSMTP)
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.fake.test")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.test")
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.example.test")
    monkeypatch.setattr(settings, "subscription_token_secret", "test-secret-not-for-production")
    yield FakeSMTP.outbox


def _text(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def _token(msg, kind: str) -> str:
    m = re.search(rf"/subscriptions/{kind}\?token=([^\s\"<&]+)", _text(msg))
    assert m, f"no {kind} link in: {_text(msg)!r}"
    return m.group(1)


@pytest.fixture()
def client(db_session):
    from api.main import app

    add_station(db_session, "a")
    add_station(db_session, "b")
    return TestClient(app)


def _subscribe(client, **overrides):
    body = {"email": "school@example.com", "station_ids": ["openaq:a"], "pollutants": ["pm25"], **overrides}
    return client.post("/api/v1/subscriptions", json=body)


def _row(db_session, email="school@example.com") -> AlertSubscription | None:
    db_session.expire_all()
    return db_session.execute(select(AlertSubscription).where(AlertSubscription.email == email)).scalar_one_or_none()


def _confirmed(client, outbox, **overrides) -> None:
    assert _subscribe(client, **overrides).status_code == 202
    assert client.post("/api/v1/subscriptions/confirm", json={"token": _token(outbox[-1], "confirm")}).status_code == 200


# ------------------------------------------------------------- double opt-in

def test_nothing_is_active_until_the_mailbox_owner_confirms(client, db_session, outbox):
    resp = _subscribe(client)
    assert resp.status_code == 202
    assert "subscriber_id" not in resp.text  # no credential in any response
    row = _row(db_session)
    assert row.is_active is False and row.is_confirmed is False
    assert len(outbox) == 1 and outbox[0]["To"] == "school@example.com"

    token = _token(outbox[0], "confirm")
    assert token not in (row.confirm_token_hash or "")  # only the hash is stored
    assert client.post("/api/v1/subscriptions/confirm", json={"token": token}).json()["status"] == "confirmed"
    row = _row(db_session)
    assert row.is_active is True and row.is_confirmed is True
    # Single use.
    assert client.post("/api/v1/subscriptions/confirm", json={"token": token}).status_code == 400


def test_a_stranger_cannot_change_a_confirmed_subscription(client, db_session, outbox):
    _confirmed(client, outbox)
    before = len(outbox)
    resp = _subscribe(client, station_ids=["openaq:b"], pollutants=["no2"])
    assert resp.status_code == 202
    row = _row(db_session)
    assert row.station_ids == ["openaq:a"] and row.pollutants == ["pm25"]
    # The owner is mailed a manage link instead; only that link can change it.
    manage = _token(outbox[before], "manage")
    resp = client.post("/api/v1/subscriptions/manage",
                       json={"token": manage, "station_ids": ["openaq:b"], "pollutants": ["no2"]})
    assert resp.status_code == 200
    assert _row(db_session).station_ids == ["openaq:b"]


def test_response_is_identical_for_new_and_known_addresses(client, outbox):
    new = _subscribe(client, email="new@example.com").json()
    _confirmed(client, outbox, email="known@example.com")
    known = _subscribe(client, email="known@example.com").json()
    assert new == known


def test_confirmation_emails_are_rate_limited_per_address(client, outbox):
    for _ in range(5):
        assert _subscribe(client).status_code == 202
    assert len(outbox) == get_settings().subscription_emails_per_hour


def test_rejects_unknown_station_and_bad_input(client):
    assert _subscribe(client, station_ids=["openaq:typo"]).status_code == 422
    assert _subscribe(client, station_ids=[]).status_code == 422
    assert _subscribe(client, station_ids=[f"s{i}" for i in range(11)]).status_code == 422
    assert _subscribe(client, pollutants=["PM25"]).status_code == 422
    assert _subscribe(client, email="not-an-email").status_code == 422


def test_normalises_email_and_dedupes(client, db_session):
    _subscribe(client, email="  Mixed.Case@Example.COM ", station_ids=["openaq:a", "openaq:a"], pollutants=["pm25", "pm25"])
    row = _row(db_session, "mixed.case@example.com")
    assert row.station_ids == ["openaq:a"] and row.pollutants == ["pm25"]


# ------------------------------------------------------- unsubscribe / delete

def _unsubscribe_token(db_session) -> str:
    from alerting.tokens import make_unsubscribe_token

    return make_unsubscribe_token(_row(db_session).subscriber_id)


def test_unsubscribe_link_switches_off_and_starts_retention_clock(client, db_session, outbox):
    _confirmed(client, outbox)
    token = _unsubscribe_token(db_session)
    assert client.post("/api/v1/subscriptions/unsubscribe", json={"token": token}).json()["status"] == "unsubscribed"
    row = _row(db_session)
    assert row.is_active is False and row.unsubscribed_at is not None
    forged = token[:-4] + "AAAA"
    assert client.post("/api/v1/subscriptions/unsubscribe", json={"token": forged}).status_code == 400


def test_one_click_unsubscribe_needs_the_rfc8058_body(client, db_session, outbox):
    _confirmed(client, outbox)
    url = f"/api/v1/subscriptions/unsubscribe/one-click?token={_unsubscribe_token(db_session)}"
    assert client.post(url, content="").status_code == 400
    resp = client.post(url, content="List-Unsubscribe=One-Click",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200 and _row(db_session).is_active is False


def test_delete_my_data_erases_the_row_and_its_history(client, db_session, outbox):
    _confirmed(client, outbox)
    row = _row(db_session)
    db_session.add(DigestLog(subscriber_id=row.subscriber_id, digest_date=DIGEST_NOW.date(),
                             sent_at=DIGEST_NOW, status=AlertStatus.SENT))
    db_session.commit()
    token = _unsubscribe_token(db_session)
    assert client.post("/api/v1/subscriptions/delete", json={"token": token}).json()["status"] == "deleted"
    assert _row(db_session) is None
    assert db_session.execute(select(DigestLog)).first() is None
    assert client.post("/api/v1/subscriptions/delete", json={"token": token}).status_code == 400


def test_missing_token_secret_fails_loudly_rather_than_guessing(monkeypatch):
    from alerting.tokens import make_unsubscribe_token

    monkeypatch.setattr(get_settings(), "subscription_token_secret", "")
    with pytest.raises(RuntimeError, match="SUBSCRIPTION_TOKEN_SECRET"):
        make_unsubscribe_token(__import__("uuid").uuid4())


# ------------------------------------------------------------------ retention

def test_retention_purge(client, db_session, outbox):
    from alerting.retention import purge_subscriptions

    _subscribe(client, email="stale@example.com")  # never confirmed
    _subscribe(client, email="fresh@example.com")  # never confirmed, recent
    _confirmed(client, outbox, email="gone@example.com")
    _confirmed(client, outbox, email="active@example.com")
    now = datetime.now(UTC)
    _row(db_session, "stale@example.com").created_at = now - timedelta(days=8)
    db_session.commit()  # _row() expires the session, which would drop an unsaved change
    gone = _row(db_session, "gone@example.com")
    gone.is_active, gone.unsubscribed_at = False, now - timedelta(days=91)
    db_session.commit()

    assert purge_subscriptions(db_session, now) == {"unconfirmed_deleted": 1, "unsubscribed_deleted": 1}
    left = {r.email for r in db_session.execute(select(AlertSubscription)).scalars()}
    assert left == {"fresh@example.com", "active@example.com"}


# --------------------------------------------------------------------- digest

def _digest(db_session):
    from alerting.digest import send_daily_digests

    return send_daily_digests(db_session, DIGEST_NOW)


def test_digest_states_every_verdict_including_no_data(client, db_session, outbox):
    made_at = DIGEST_NOW.replace(minute=0) - timedelta(hours=2)
    seed_forecast_run(db_session, "openaq:a", made_at, flagged_horizons=(24,))  # tomorrow is no-go
    # openaq:b has no forecast at all -> must say "No data", never be left out.
    _confirmed(client, outbox, station_ids=["openaq:a", "openaq:b"])
    _subscribe(client, email="pending@example.com")  # unconfirmed: gets nothing
    outbox.clear()

    assert _digest(db_session) == {"sent": 1, "failed": 0, "skipped": 0, "not_invited": 0}
    assert len(outbox) == 1
    msg = outbox[0]
    body = _text(msg)
    assert msg["To"] == "school@example.com"
    assert "Sat 26 Sep" in msg["Subject"] and "no-go for 1 of 2" in msg["Subject"]
    assert "Station a (PM2.5)" in body and "Sat 26 Sep: No-go" in body
    assert "Station b (PM2.5)" in body and "No data" in body
    assert "do not treat it as safe" in body
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/subscriptions/unsubscribe?token=" in body


def test_digest_is_sent_once_per_day_and_failed_sends_are_retried(client, db_session, outbox):
    _confirmed(client, outbox)
    outbox.clear()
    FakeSMTP.fail = True
    assert _digest(db_session) == {"sent": 0, "failed": 1, "skipped": 0, "not_invited": 0}
    FakeSMTP.fail = False
    assert _digest(db_session) == {"sent": 1, "failed": 0, "skipped": 0, "not_invited": 0}
    assert _digest(db_session) == {"sent": 0, "failed": 0, "skipped": 1, "not_invited": 0}
    assert len(outbox) == 1
    log = db_session.execute(select(DigestLog)).scalar_one()
    assert log.status == AlertStatus.SENT and log.digest_date.isoformat() == "2026-09-26"


def test_digest_without_smtp_is_recorded_as_failed_not_sent(client, db_session, outbox, monkeypatch):
    _confirmed(client, outbox)
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    assert _digest(db_session)["failed"] == 1


def test_digest_uses_the_same_verdicts_as_the_website(client, db_session, outbox):
    from db.models import Station
    from models.outlook import build_outlook

    made_at = DIGEST_NOW.replace(minute=0) - timedelta(hours=2)
    seed_forecast_run(db_session, "openaq:a", made_at, probability=0.2)  # caution every day
    outlook = build_outlook(db_session, db_session.get(Station, "openaq:a"), Pollutant.PM25, now=DIGEST_NOW)
    _confirmed(client, outbox)
    outbox.clear()
    _digest(db_session)
    tomorrow = (DIGEST_NOW + timedelta(days=1)).date()
    assert outlook.verdict_for(tomorrow) == "caution"
    assert "Sat 26 Sep: Caution" in _text(outbox[0])


# ------------------------------------------------------------------ demo mode

def test_demo_mode_only_invited_addresses_can_subscribe_and_strangers_leave_no_trace(
    client, db_session, outbox, monkeypatch
):
    monkeypatch.setattr(get_settings(), "subscription_allowed_emails", " Owner@Example.com , friend@example.com")

    invited = _subscribe(client, email="owner@example.com")
    stranger = _subscribe(client, email="school@example.com")
    assert invited.status_code == stranger.status_code == 202
    assert invited.json() == stranger.json()  # the reply must not reveal who is invited
    assert [m["To"] for m in outbox] == ["owner@example.com"]
    assert _row(db_session, "school@example.com") is None  # nothing stored about the stranger
    assert _row(db_session, "owner@example.com") is not None


def test_availability_says_whether_sign_ups_are_open_without_listing_anyone(client, monkeypatch):
    assert client.get("/api/v1/subscriptions/availability").json()["open"] is True
    monkeypatch.setattr(get_settings(), "subscription_allowed_emails", "owner@example.com")
    body = client.get("/api/v1/subscriptions/availability").json()
    assert body["open"] is False and "demo" in body["message"]
    assert "owner@example.com" not in str(body)


def test_digest_skips_a_confirmed_address_that_is_no_longer_invited(client, db_session, outbox, monkeypatch):
    _confirmed(client, outbox)  # subscribed while sign-ups were open
    outbox.clear()
    monkeypatch.setattr(get_settings(), "subscription_allowed_emails", "owner@example.com")
    assert _digest(db_session) == {"sent": 0, "failed": 0, "skipped": 0, "not_invited": 1}
    assert outbox == []
