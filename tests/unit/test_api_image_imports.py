"""The API Docker image is deliberately minimal: it has no `requests` (that is an
ingestion/Airflow dependency). A router that imports an ingestion module which
imports `requests` passes every test in the full virtualenv and then crashes the
container on startup (this happened on 2026-09-21). Import the app with
`requests` made unavailable, as it is in the image.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_api_app_imports_without_requests_installed():
    blocked = ["requests", "xarray", "cdsapi", "airflow", "jinja2"]  # installed locally, absent from the API image
    code = f"import sys; [sys.modules.__setitem__(m, None) for m in {blocked!r}]; import api.main; print('ok')"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-1500:]
