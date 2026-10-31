"""
Tests for the archive rule of AI analysis requests (app/archive_rules.py),
written BEFORE the code (TDD - brief 4.2, Red/Green/Refactor commits).

The rule: an analysis request is archived once AI_REQUEST_ARCHIVE_DAYS have
passed since it finished (default 30; fractions allowed; 0 = never). A
request that has not finished is never archived. "Passed" includes the exact
moment of the limit - the same as NOTIFICATION_ARCHIVE_DAYS (app/sites.py).
A negative or non-numeric setting stops startup with a clear ValueError.

Every expected value below was worked out by hand from that rule, not by
running the code.

Run from backend/:  python -m pytest tests/test_archive_rules.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.archive_rules import is_request_archived, request_archive_after_from_env

ENV = "AI_REQUEST_ARCHIVE_DAYS"
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
FINISHED = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
THIRTY_DAYS = 30 * DAY


# ---------------------------------------------------------------------------
# is_request_archived(finished_at, now, after)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("now, expected", [
    (FINISHED, False),                              # it just finished
    (FINISHED + THIRTY_DAYS - SECOND, False),       # one second before the limit
    (FINISHED + THIRTY_DAYS, True),                 # exactly at the limit
    (FINISHED + THIRTY_DAYS + SECOND, True),        # one second after
    (FINISHED + 400 * DAY, True),                   # long after
])
def test_a_finished_request_is_archived_from_the_limit_on(now, expected):
    assert is_request_archived(FINISHED, now, THIRTY_DAYS) is expected


@pytest.mark.parametrize("now", [FINISHED, FINISHED + 10_000 * DAY])
def test_an_unfinished_request_is_never_archived(now):
    assert is_request_archived(None, now, THIRTY_DAYS) is False


def test_no_limit_means_never_archived():          # AI_REQUEST_ARCHIVE_DAYS=0
    assert is_request_archived(FINISHED, FINISHED + 10_000 * DAY, None) is False


@pytest.mark.parametrize("hours_later, expected", [(11.99, False), (12, True)])
def test_a_fraction_of_a_day_works(hours_later, expected):
    half_a_day = timedelta(hours=12)
    assert is_request_archived(FINISHED, FINISHED + timedelta(hours=hours_later), half_a_day) is expected


# ---------------------------------------------------------------------------
# request_archive_after_from_env()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, THIRTY_DAYS),                 # not set: the default
    ("", THIRTY_DAYS),                   # set but empty: the default
    ("7", 7 * DAY),
    (" 10 ", 10 * DAY),                  # spaces around it are fine
    ("0.5", timedelta(hours=12)),        # fractions of a day
    ("0", None),                         # 0 = never archive
])
def test_the_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert request_archive_after_from_env() == expected


@pytest.mark.parametrize("value", ["-1", "-0.5", "soon", "nan", "inf", "1e10"])
def test_a_bad_setting_stops_startup_with_a_clear_message(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(ValueError, match="AI_REQUEST_ARCHIVE_DAYS must be a number of days"):
        request_archive_after_from_env()
