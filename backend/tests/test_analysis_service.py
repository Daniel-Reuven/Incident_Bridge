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
