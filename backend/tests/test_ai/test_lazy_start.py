"""
Tests for starting the AI side on demand (v0.4.5): the worker thread and
the provider (and its HTTP client) are created on the FIRST analysis request,
not at server startup - except when jobs were restored from SQLite.

See app/ai/worker.py ("Started on demand"), app/ai/providers/loader.py and
app/ai/setup.py.

Run from backend/:  python -m pytest tests/test_ai/test_lazy_start.py -v
"""

import threading

import pytest

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers import ProviderLoader
from app.ai.providers.fake_provider import FakeProvider
from app.ai.setup import create_analysis_worker
from app.ai.worker import THREAD_NAME, AnalysisWorker

from tests.test_ai.conftest import wait_for_analysis


class CountingBuild:
    """A `build` function for ProviderLoader that counts how often the provider is created."""

    def __init__(self, provider=None, error=None):
        self.provider = provider or FakeProvider()
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.provider


def worker_threads():
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


def test_nothing_starts_until_the_first_request(make_worker, make_fault, state):
    build = CountingBuild()
    worker = make_worker(ProviderLoader("Fake AI", ["fake-model"], build), lazy=True)
    assert not worker.started and not worker.provider_loaded and build.calls == 0
    assert worker_threads() == []
    # The page's data is available from the settings without creating the provider.
    assert (worker.provider_name, worker.models) == ("Fake AI", ["fake-model"])
    assert build.calls == 0

    fault = make_fault()
    worker.request(fault, state.users.get("tech1"))
    assert worker.started and len(worker_threads()) == 1
    wait_for_analysis(worker, fault)
    assert worker.provider_loaded and build.calls == 1


def test_the_provider_is_created_once_and_the_thread_keeps_running(make_worker, make_fault, state):
    build = CountingBuild()
    worker = make_worker(ProviderLoader("Fake AI", ["fake-model"], build), lazy=True)
    for title in ("One", "Two"):
        fault = make_fault(title)
        worker.request(fault, state.users.get("tech1"))
        wait_for_analysis(worker, fault)
    assert build.calls == 1
    assert worker.is_alive()                     # idle, but never stops on its own


def test_two_simultaneous_first_requests_start_exactly_one_thread(make_worker, make_fault, state):
    worker = make_worker(FakeProvider(delay_seconds=0.2), lazy=True)
    faults = [make_fault(f"F{i}") for i in range(2)]
    barrier = threading.Barrier(2)
    errors = []

    def ask(fault):
        barrier.wait()
        try:
            worker.request(fault, state.users.get("tech1"))
        except Exception as exc:                 # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=ask, args=(fault,)) for fault in faults]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(worker_threads()) == 1


def test_a_refused_first_request_starts_nothing(make_worker, make_fault, state):
    from app.models import IncidentStatus
    worker = make_worker(lazy=True)
    fault = make_fault()
    fault.status = IncidentStatus.CLOSED
    with pytest.raises(RuntimeError):
        worker.request(fault, state.users.get("tech1"))
    assert not worker.started


def test_restored_jobs_start_the_worker_at_startup(make_worker, make_fault, state, db_path, broadcaster,
                                                   monkeypatch):
    fault = make_fault()
    make_worker(start=False).request(fault, state.users.get("tech1"))     # saved, never run
    monkeypatch.setenv("AI_PROVIDER", "fake")
    monkeypatch.setenv("AI_REQUEST_DELAY_SECONDS", "0")
    worker = create_analysis_worker(state, broadcaster, db_path)          # a "restart"
    try:
        assert worker.started
        wait_for_analysis(worker, fault)
    finally:
        worker.stop()


def test_startup_without_saved_jobs_starts_nothing(state, db_path, broadcaster, monkeypatch, ai_log):
    monkeypatch.setenv("AI_PROVIDER", "fake")
    worker = create_analysis_worker(state, broadcaster, db_path)
    try:
        assert not worker.started and not worker.provider_loaded
        assert worker_threads() == []
        assert "the worker starts on the first request" in ai_log.text
    finally:
        worker.stop()                            # logs "was never started"
    assert "was never started" in ai_log.text


def test_the_thread_loads_the_provider_then_refreshes_models(make_worker, make_fault, state, ai_log):
    order = []

    class Refreshing(FakeProvider):
        def refresh_models(self):
            order.append("refresh")
            self.models = ["newer-model"] + self.models
            return "refreshed"

    def build():
        order.append("build")
        return Refreshing()

    worker = make_worker(ProviderLoader("Fake AI", ["fake-model"], build), lazy=True)
    fault = make_fault()
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert order == ["build", "refresh"]
    assert worker.models == ["newer-model", "fake-model"]                # the live list from now on
    assert "Created the Fake AI provider and its HTTP client" in ai_log.text


def test_a_provider_that_cannot_be_created_posts_an_error_and_the_thread_survives(make_worker, make_fault, state):
    build = CountingBuild(error=RuntimeError("client failed"))
    worker = make_worker(ProviderLoader("Fake AI", ["fake-model"], build), lazy=True)
    fault = make_fault()
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    answer = [c.text for c in fault.comments if c.author.username == "ai-assistant"][0]
    assert "could not be started" in answer
    assert worker.is_alive()
    build.error = None                            # fixed: the next job tries again and works
    second = make_fault("Second")
    worker.request(second, state.users.get("tech1"))
    wait_for_analysis(worker, second)
    assert "could not be started" not in second.comments[-1].text


def test_loader_label_matches_the_provider_label():
    provider = FakeProvider(models=["a", "b"])
    loader = ProviderLoader("Fake AI", ["a", "b"], lambda: provider)
    assert loader.label() == provider.label() == "Fake AI (models: a > b)"
    assert loader.label("b") == provider.label("b")
    assert not loader.loaded and loader.get() is provider and loader.loaded


def test_a_worker_built_with_a_ready_provider_still_works_the_old_way(state, db_path, broadcaster):
    worker = AnalysisWorker(state, FakeProvider(), SqliteAnalysisJobStore(db_path), broadcaster, 0)
    assert worker.provider_loaded and not worker.started
    worker.stop()
