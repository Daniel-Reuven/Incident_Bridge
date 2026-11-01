"""
Shared fixtures for the AI analysis tests (tests/test_ai/). None of them need
an API key or network access: they use FakeProvider
(app/ai/providers/fake_provider.py).
"""

import time

import pytest

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers.fake_provider import FakeProvider
from app.ai.worker import AnalysisWorker
from app.events import EventBroadcaster
from app.models import Fault, SeverityCategory
from app.state import AppState
from tests.install_helper import add_demo_users


class RecordingBroadcaster(EventBroadcaster):
    """An EventBroadcaster that just remembers what was published (no event loop, no tabs)."""

    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event, exclude_client_id=None, roles=None):
        self.events.append(event)

    def incident_events(self, action=None):
        return [e for e in self.events if "scope" not in e and (action is None or e["action"] == action)]


def wait_until(condition, timeout=5.0):
    """Poll `condition()` until it is true (the worker runs in its own thread). Fails the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def wait_for_analysis(worker, *faults, timeout=5.0):
    """
    Wait until every fault in `faults` has had its analysis FINISHED by `worker`.

    Waiting only for the first AI comment is not enough: the worker adds the
    answer's comments one by one, then records the success and publishes the
    event, and only then marks the job finished (AnalysisWorker._finish()).
    So this waits for both an AI comment AND the job being done ("idle") -
    after that, every part, the success record and the event are in place.
    """
    from app.models.system_users import AI_USERNAME

    def done():
        return all(any(c.author.username == AI_USERNAME for c in fault.comments)
                   and worker.status_of(fault.id)["state"] == "idle" for fault in faults)

    wait_until(done, timeout)


@pytest.fixture
def ai_log(caplog):
    """
    The AI console log lines (app/ai/log.py) written during the test, as text.
    The AI logger does not propagate to the root logger, so caplog's handler
    is attached to it directly.
    """
    from app.ai.log import log
    log.addHandler(caplog.handler)
    caplog.set_level("DEBUG", logger=log.name)
    yield caplog
    log.removeHandler(caplog.handler)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "ai.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.delenv("INCIDENT_BRIDGE_USERS", raising=False)
    return path


@pytest.fixture
def state(db_path):
    """A real AppState (users admin + tech1 + the ai-assistant system user) on a throwaway SQLite file."""
    return add_demo_users(AppState.create())


@pytest.fixture
def make_fault(state):
    def make(title="Checkout failing", status=None):
        fault = Fault(title=title, description="Payments fail for some users.", created_by=state.users.get("tech1"),
                      severity=SeverityCategory.MAJOR, severity_score=2.0, details={"performance_degraded": True})
        state.incidents.add(fault)
        state.faults.push(fault)
        return fault
    return make


@pytest.fixture
def broadcaster():
    return RecordingBroadcaster()


@pytest.fixture
def make_worker(state, db_path, broadcaster):
    """
    Build a worker; every worker built here is stopped after the test.
    start=True   start the thread now;
    start=False  never start it by itself (not even on a request) - to inspect the queue;
    lazy=True    the app's behaviour: not started, starts on the first request.
    `provider` may be an AnalysisProvider or a ProviderLoader.
    """
    workers = []

    def make(provider=None, delay_seconds=0.0, start=True, cooldown_seconds=300.0, lazy=False):
        worker = AnalysisWorker(state, provider or FakeProvider(), SqliteAnalysisJobStore(db_path),
                                broadcaster, delay_seconds, cooldown_seconds, start_on_request=lazy)
        start = start and not lazy
        workers.append(worker)
        if start:
            worker.start()
        return worker

    yield make
    for worker in workers:
        worker.stop()
