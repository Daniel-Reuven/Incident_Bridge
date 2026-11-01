"""
Unit tests for app/analysis_service.py's Observer plumbing: a listener that
raises is logged and never stops the other listeners (or whoever is
dispatching the event - app/ai/worker.py never sees the exception).

Run from backend/:  python -m pytest tests/test_analysis_service.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.ai.result import AnalysisFindings
from app.analysis_service import AnalysisCompleted, notify_listeners
from app.models import Fault, SeverityCategory


class RaisingListener:
    def on_analysis_completed(self, event):
        raise RuntimeError("boom")


class RecordingListener:
    def __init__(self):
        self.events = []

    def on_analysis_completed(self, event):
        self.events.append(event)


@pytest.fixture
def ai_log(caplog):
    from app.ai.log import log
    log.addHandler(caplog.handler)
    caplog.set_level("DEBUG", logger=log.name)
    yield caplog
    log.removeHandler(caplog.handler)


@pytest.fixture
def event(admin_user) -> AnalysisCompleted:
    fault = Fault(title="Checkout failing", description="x", created_by=admin_user,
                  severity=SeverityCategory.MAJOR, severity_score=1.0, details={})
    return AnalysisCompleted(fault, AnalysisFindings("major", (1007,), 0.9), "Fake AI", "fake-model")


def test_a_failing_listener_is_logged_and_does_not_stop_the_others(event, ai_log):
    recorder = RecordingListener()
    notify_listeners([RaisingListener(), recorder], event)   # must not raise

    assert recorder.events == [event]
    assert "boom" in ai_log.text
    assert event.incident.id in ai_log.text


def test_every_listener_runs_even_when_every_one_raises(event, ai_log):
    notify_listeners([RaisingListener(), RaisingListener()], event)   # must not raise
    assert ai_log.text.count("boom") == 2


def test_no_listeners_is_fine(event):
    notify_listeners([], event)   # must not raise


class RecordingBroadcaster:
    def __init__(self):
        self.published = []

    def publish(self, event, exclude_client_id=None, roles=None):
        self.published.append((event, roles))


def test_an_ai_triggered_check_is_published_like_one_from_the_portal(event, admin_user, caplog):
    """So it is in the activity log (site.site_checked by ai-assistant) and open portal pages update."""
    from app.audit import record_published_event
    from app.services.availability import FakeChecker
    from app.sites import SiteDirectory
    from app.analysis_service import SiteImpactListener

    directory = SiteDirectory(checker=FakeChecker({"https://hr.example.com": [503]}))
    site = directory.create_site(admin_user, "HR portal", "https://hr.example.com")
    broadcaster = RecordingBroadcaster()
    found = AnalysisCompleted(event.incident, AnalysisFindings("major", (site.site_id,), 0.9), "Fake AI", "m")
    SiteImpactListener(directory, broadcaster).on_analysis_completed(found)

    [(published, roles)] = broadcaster.published
    assert roles == {"admin"}
    assert published["scope"] == "sites" and published["action"] == "site_checked"
    assert published["actor"] == "ai-assistant" and published["site_id"] == site.site_id
    assert published["status"] == site.status.value and published["changed"] is True
    caplog.set_level("INFO", logger="incident_bridge.audit")
    record_published_event(published)
    record = next(r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "site.site_checked")
    assert record["user"] == "ai-assistant" and event.incident.id in record["reason"]


def test_without_a_broadcaster_the_check_still_runs(event, admin_user):
    from app.services.availability import FakeChecker
    from app.sites import SiteDirectory
    from app.analysis_service import SiteImpactListener

    checker = FakeChecker({"https://hr.example.com": [200]})
    directory = SiteDirectory(checker=checker)
    site = directory.create_site(admin_user, "HR portal", "https://hr.example.com")
    found = AnalysisCompleted(event.incident, AnalysisFindings("major", (site.site_id,), 0.9), "Fake AI", "m")
    SiteImpactListener(directory).on_analysis_completed(found)
    assert checker.calls == ["https://hr.example.com"]
