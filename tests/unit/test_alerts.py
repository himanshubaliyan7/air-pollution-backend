from unittest.mock import patch

from common.config import get_settings
from orchestration.plugins.common import alerts
from orchestration.plugins.common.alerts import redact, send_telegram


def test_redact_removes_api_keys_and_bot_tokens():
    url = "HTTPError for url: https://api.data.gov.in/resource/x?api-key=SECRET123&format=json"
    assert "SECRET123" not in redact(url) and "api-key=<redacted>" in redact(url)
    assert "AbC-123" not in redact("https://api.telegram.org/bot12345:AbC-123_xyz/sendMessage")
    assert "KEY999" not in redact("headers {'X-API-Key': 'KEY999'}")


def test_telegram_is_a_silent_noop_until_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "")
    with patch.object(alerts.requests, "post") as post:
        assert send_telegram("hi") is False
        post.assert_not_called()


def test_telegram_posts_a_redacted_message_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "1:tok")
    monkeypatch.setattr(get_settings(), "telegram_chat_id", "42")
    with patch.object(alerts.requests, "post") as post:
        post.return_value.status_code = 200
        assert send_telegram("boom ?api-key=SECRET") is True
        body = post.call_args.kwargs["json"]
        assert body["chat_id"] == "42" and "SECRET" not in body["text"]


def test_telegram_failures_never_raise(monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "1:tok")
    monkeypatch.setattr(get_settings(), "telegram_chat_id", "42")
    with patch.object(alerts.requests, "post", side_effect=alerts.requests.ConnectionError("down")):
        assert send_telegram("x") is False


def test_failure_callback_reports_dag_task_and_error_and_survives_bad_context(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)

    class TI:
        dag_id, task_id, try_number = "ingestion_dag", "fetch_openaq_readings", 2

    alerts.notify_task_failure({"task_instance": TI(), "exception": RuntimeError("401")})
    assert "ingestion_dag.fetch_openaq_readings" in sent[0] and "RuntimeError" in sent[0]
    alerts.notify_task_failure({})  # must not raise


def test_digest_run_with_a_failed_email_is_reported():
    from orchestration.plugins.common.tasks import digest_problem

    assert digest_problem({"sent": 3, "failed": 0, "skipped": 0, "not_invited": 2}) is None
    assert digest_problem({"sent": 0, "failed": 0, "skipped": 0, "not_invited": 0}) is None
    # A retry after a partial failure: the mended ones show up as skipped.
    assert digest_problem({"sent": 1, "failed": 0, "skipped": 2, "not_invited": 0}) is None
    assert "2 of 5" in digest_problem({"sent": 2, "failed": 2, "skipped": 1, "not_invited": 4})
    assert "1 of 1" in digest_problem({"sent": 0, "failed": 1, "skipped": 0, "not_invited": 0})
