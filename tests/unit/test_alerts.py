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


def test_failure_alert_says_whether_it_is_a_repeat_and_keeps_the_whole_message(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    monkeypatch.setattr(alerts, "_run_history", lambda dag_id, run_id: f"history of {dag_id} {run_id}")

    class TI:
        dag_id, task_id, try_number, run_id = "evaluation_monitoring_dag", "evaluate_and_check_drift", 2, "r1"

    long = "x" * 1000 + " END"
    alerts.notify_task_failure({"task_instance": TI(), "exception": RuntimeError(long)})
    assert "history of evaluation_monitoring_dag r1" in sent[0] and sent[0].endswith(" END")

    # The history lookup failing must not cost the alert.
    monkeypatch.setattr(alerts, "_run_history", lambda *a: 1 / 0)
    alerts.notify_task_failure({"task_instance": TI(), "exception": RuntimeError("boom")})
    assert sent[1].endswith("RuntimeError: boom")


def test_history_line_counts_the_failed_runs_in_a_row():
    from datetime import datetime

    ok = datetime(2026, 10, 15, 0, 5)
    assert alerts.history_line(["success", "failed"], ok) == "first failure after a good run; last success 2026-10-15 00:05 UTC"
    assert alerts.history_line(["failed", "failed", "success"], ok).startswith("failed run 3 in a row")
    assert alerts.history_line([], None) == "first failure after a good run; no successful run on record"


def test_count_faults_flag_repeated_nights_and_impossible_totals():
    from datetime import datetime

    from orchestration.plugins.common.regional import count_faults_by_region

    night = datetime(2026, 10, 19, 0, 0)
    retry = datetime(2026, 10, 19, 0, 10)
    row = lambda sid, at, n=1, recall=1.0: (28.6, 77.2, sid, "pm25", at, n, recall)  # noqa: E731 - Delhi
    # One row per station and night: nothing wrong.
    assert count_faults_by_region([row("a", night), row("b", night)], 7) == []
    # A retry stored the night again.
    fault = count_faults_by_region([row("a", night), row("a", retry)], 7)
    assert len(fault) == 1 and "1 rows repeat" in fault[0] and fault[0].startswith("Delhi NCR")
    # One row claiming 24 days (scored on every hourly run): more than 1 pair x 7 days allows.
    assert "24 station-days counted" in count_faults_by_region([row("a", night, n=24, recall=0.5)], 7)[0]


def test_digest_run_with_a_failed_email_is_reported():
    from orchestration.plugins.common.tasks import digest_problem

    assert digest_problem({"sent": 3, "failed": 0, "skipped": 0, "not_invited": 2}) is None
    assert digest_problem({"sent": 0, "failed": 0, "skipped": 0, "not_invited": 0}) is None
    # A retry after a partial failure: the mended ones show up as skipped.
    assert digest_problem({"sent": 1, "failed": 0, "skipped": 2, "not_invited": 0}) is None
    assert "2 of 5" in digest_problem({"sent": 2, "failed": 2, "skipped": 1, "not_invited": 4})
    assert "1 of 1" in digest_problem({"sent": 0, "failed": 1, "skipped": 0, "not_invited": 0})
