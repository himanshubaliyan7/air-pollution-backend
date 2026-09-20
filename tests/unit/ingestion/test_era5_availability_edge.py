"""Regression test: CDS returns HTTP 400 "latest date available for this
dataset is: ..." for a window past ERA5's ~5 day publication edge (seen live
2026-09-20 - it made every hourly ingestion run fail). ERA5Client.fetch must
slide the window back to that edge and retry rather than propagate the error.
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from ingestion.weather.era5_client import ERA5Client

_MSG = (
    "400 Client Error: Bad Request for url: https://cds.example/execution\n"
    "invalid request\nNone of the data you have requested is available yet, please "
    "revise the period requested. The latest date available for this dataset is: 2026-09-15 15:00"
)


class _FakeCDS:
    def __init__(self, errors):
        self.errors = list(errors)
        self.requests = []

    def retrieve(self, dataset, request, target):
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)


def _client(errors):
    fake = _FakeCDS(errors)
    client = ERA5Client(client=fake)
    client._parse = lambda path, now: ["parsed"]  # skip NetCDF parsing
    return client, fake


def test_window_past_edge_is_shifted_back_and_retried():
    client, fake = _client([requests.HTTPError(_MSG)])
    end = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    result = client.fetch(end - timedelta(hours=48), end)

    assert result == ["parsed"]
    assert len(fake.requests) == 2
    assert fake.requests[0]["day"] == ["18", "19", "20"]
    assert fake.requests[1]["day"] == ["13", "14", "15"]  # 48h ending at 09-15 15:00


def test_unrelated_http_error_propagates():
    client, fake = _client([requests.HTTPError("500 Server Error")])
    end = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    with pytest.raises(requests.HTTPError):
        client.fetch(end - timedelta(hours=48), end)
    assert len(fake.requests) == 1


def test_second_failure_propagates():
    client, _ = _client([requests.HTTPError(_MSG), requests.HTTPError(_MSG)])
    end = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    with pytest.raises(requests.HTTPError):
        client.fetch(end - timedelta(hours=48), end)
