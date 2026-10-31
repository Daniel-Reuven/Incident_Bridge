"""
Archive rule for AI analysis requests.

An analysis request is archived once AI_REQUEST_ARCHIVE_DAYS have passed
since it finished (default 30; fractions allowed; 0 = never). A request that
has not finished is never archived. Archived requests are reported apart
from the live counts.

Tests: tests/test_archive_rules.py (written first - TDD).
"""

import math
import os
from datetime import datetime, timedelta
from typing import Optional

REQUEST_ARCHIVE_ENV = "AI_REQUEST_ARCHIVE_DAYS"
DEFAULT_REQUEST_ARCHIVE_DAYS = 30.0


def is_request_archived(finished_at: Optional[datetime], now: datetime, after: Optional[timedelta]) -> bool:
    """True once `after` has passed since `finished_at`; never for an unfinished request or with no limit."""
    if finished_at is None or after is None:
        return False
    return now >= finished_at + after


def request_archive_after_from_env() -> Optional[timedelta]:
    """AI_REQUEST_ARCHIVE_DAYS as a timedelta (None = never). Raises ValueError for a bad value."""
    raw = (os.environ.get(REQUEST_ARCHIVE_ENV) or "").strip()
    if not raw:
        return timedelta(days=DEFAULT_REQUEST_ARCHIVE_DAYS)
    try:
        days = float(raw)
        if not days >= 0 or math.isinf(days):
            raise ValueError
        return timedelta(days=days) if days > 0 else None
    except (ValueError, OverflowError):
        raise ValueError(f"{REQUEST_ARCHIVE_ENV} must be a number of days, 0 or more "
                         f"(0 = never archive), got {raw!r}") from None
