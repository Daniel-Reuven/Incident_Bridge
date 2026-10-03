"""
Unit tests for app.context.SiteCheckSession - the context manager that locks
a site for the duration of one availability check.

Run from backend/:  python -m pytest tests/test_site_check_session.py -v
"""

import pytest

from app.context import SiteCheckSession
from app.models import Site


@pytest.fixture
def site() -> Site:
    return Site(1042, "Portal", "https://example.com")


def test_the_site_is_locked_only_inside_the_block(site):
    assert not SiteCheckSession.is_running(1042)
    with SiteCheckSession(site) as session:
        assert SiteCheckSession.is_running(1042)
        assert session.duration_ms is None
    assert not SiteCheckSession.is_running(1042)
    assert session.duration_ms >= 0


def test_a_second_check_of_the_same_site_is_refused(site):
    other_object_same_site = Site(1042, "Same id", "https://example.org")
    with SiteCheckSession(site):
        with pytest.raises(RuntimeError, match="already being checked"):
            with SiteCheckSession(other_object_same_site):
                pass
        assert SiteCheckSession.is_running(1042)   # the refused session did not release the first lock
    assert not SiteCheckSession.is_running(1042)


def test_different_sites_can_be_checked_at_the_same_time(site):
    with SiteCheckSession(site), SiteCheckSession(Site(7, "Other", "https://example.org")):
        assert SiteCheckSession.is_running(1042) and SiteCheckSession.is_running(7)


def test_the_lock_is_released_even_when_the_block_raises(site):
    with pytest.raises(ZeroDivisionError):     # never suppressed
        with SiteCheckSession(site) as session:
            1 / 0
    assert not SiteCheckSession.is_running(1042)
    assert session.duration_ms is not None


def test_str_and_repr(site):
    session = SiteCheckSession(site)
    assert str(session) == "SiteCheckSession(site=Site 1042, state=Pending)"
    with session:
        assert str(session) == "SiteCheckSession(site=Site 1042, state=Running)"
    assert str(session) == "SiteCheckSession(site=Site 1042, state=Finished)"
    assert repr(session).startswith("SiteCheckSession(site_id=1042, duration_ms=")
