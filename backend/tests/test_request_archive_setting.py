"""
AI_REQUEST_ARCHIVE_DAYS where the app uses it (the rule itself is in
tests/test_archive_rules.py, written first - TDD): a bad value stops the
start with its message (the report filter: tests/test_reports.py).

Run from backend/:  python -m pytest tests/test_request_archive_setting.py -v
"""

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.state import AppState
from tests.conftest import env_without_dotenv


@pytest.mark.parametrize("value, expected", [("", timedelta(days=30)), ("7", timedelta(days=7)), ("0", None)])
def test_the_app_reads_the_setting_at_start(monkeypatch, value, expected):
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("AI_REQUEST_ARCHIVE_DAYS", value)
    assert AppState.create().request_archive_after == expected


def test_a_bad_value_stops_the_app(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("AI_REQUEST_ARCHIVE_DAYS", "-1")
    with pytest.raises(ValueError, match="AI_REQUEST_ARCHIVE_DAYS must be a number of days"):
        AppState.create()


def test_serve_app_stops_with_the_message_and_no_traceback(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = env_without_dotenv(DATABASE_PATH=str(tmp_path / "t.db"), AI_REQUEST_ARCHIVE_DAYS="soon")
    result = subprocess.run([sys.executable, "serve_app.py"], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "AI_REQUEST_ARCHIVE_DAYS must be a number of days" in result.stderr and "Traceback" not in result.stderr

