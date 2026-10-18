"""
Tests for the AI analysis queue and its worker thread (app/ai/worker.py):
FIFO order, one request at a time, the delay between requests, dropping
closed faults, cancelling, errors, restarts, and the single named thread.

Run from backend/:  python -m pytest tests/test_ai/test_worker.py -v
"""

import threading
import time

import pytest

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers.fake_provider import FakeProvider
from app.ai.worker import MAX_QUEUED_JOBS, THREAD_NAME, AnalysisWorker
from app.models import IncidentStatus, MaintenanceTask, ResolutionType
from app.models.system_users import AI_USERNAME

from tests.test_ai.conftest import wait_for_analysis, wait_until


class GateProvider(FakeProvider):
    """A fake whose analyze() waits until the test opens the gate - to hold a job in the 'running' state."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gate = threading.Event()
        self.started = threading.Event()
        self.active = 0
        self.max_active = 0
        self.order = []

    def analyze(self, instructions, incident_data):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(time.monotonic())
        self.started.set()
        self.gate.wait(5)
        self.active -= 1
        return super().analyze(instructions, incident_data)


def ai_comments(fault):
    return [c.text for c in fault.comments if c.author.username == AI_USERNAME]


def test_a_request_posts_the_request_comment_then_the_answer(state, make_fault, make_worker, broadcaster, db_path):
    fault = make_fault()
    worker = make_worker()
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)

    texts = [c.text for c in fault.comments]
    assert texts[0] == "tech1 requested Fake AI analysis."
    assert texts[1].startswith("AI analysis by Fake AI (model: fake-model)\n\nSummary:")
    wait_until(lambda: worker.status_of(fault.id)["state"] == "idle")
    assert SqliteAnalysisJobStore(db_path).load_all() == []           # finished jobs leave SQLite
    assert len(broadcaster.incident_events("ai_analysis_posted")) == 1


def test_the_prompt_is_built_when_the_job_starts(state, make_fault, make_worker):
    provider = GateProvider()
    first, second = make_fault("First"), make_fault("Second")
    worker = make_worker(provider)
    worker.request(first, state.users.get("tech1"))
    provider.started.wait(5)
    worker.request(second, state.users.get("tech1"))
    second.add_comment(state.users.get("admin"), "Added while waiting in the queue.")
    provider.gate.set()
    wait_until(lambda: len(provider.calls) == 2)
    assert "Added while waiting in the queue." in provider.calls[1][1]


def test_jobs_run_in_fifo_order_one_at_a_time(state, make_fault, make_worker):
    provider = GateProvider()
    faults = [make_fault(f"Fault {i}") for i in range(3)]
    worker = make_worker(provider)
    for fault in faults:
        worker.request(fault, state.users.get("tech1"))
    provider.started.wait(5)
    assert worker.status_of(faults[0].id) == {"state": "running", "position": None}
    assert worker.status_of(faults[2].id) == {"state": "queued", "position": 2}
    provider.gate.set()
    wait_for_analysis(worker, *faults)
    assert provider.max_active == 1
    assert [f'"{f.title}"' in call[1] for call, f in zip(provider.calls, faults)] == [True] * 3


def test_the_delay_is_applied_between_api_calls(state, make_fault, make_worker):
    provider = GateProvider()
    provider.gate.set()
    first, second = make_fault("First"), make_fault("Second")
    worker = make_worker(provider, delay_seconds=0.4)
    worker.request(first, state.users.get("tech1"))
    worker.request(second, state.users.get("tech1"))
    wait_until(lambda: len(provider.order) == 2)
    assert provider.order[1] - provider.order[0] >= 0.4


def test_a_dropped_job_does_not_wait_for_the_delay(state, make_fault, make_worker):
    provider = GateProvider()
    provider.gate.set()
    closed, normal = make_fault("Closed later"), make_fault("Normal")
    worker = make_worker(provider, delay_seconds=3, start=False)
    worker.request(closed, state.users.get("tech1"))
    worker.request(normal, state.users.get("tech1"))
    closed.close(state.users.get("tech1"), ResolutionType.RESOLVED, "Fixed meanwhile.")
    started = time.monotonic()
    worker.start()
    wait_for_analysis(worker, normal)
    assert time.monotonic() - started < 2
    assert ai_comments(closed) == [] and len(provider.calls) == 1


def test_a_closed_fault_cannot_be_analyzed_and_a_maintenance_task_is_refused(state, make_fault, make_worker):
    fault = make_fault()
    fault.close(state.users.get("tech1"), ResolutionType.RESOLVED, "Done.")
    worker = make_worker(start=False)
    with pytest.raises(RuntimeError, match="open or in-progress"):
        worker.request(fault, state.users.get("tech1"))
    task = MaintenanceTask(title="Patch", description="d", created_by=state.users.get("admin"))
    with pytest.raises(ValueError, match="only available for faults"):
        worker.request(task, state.users.get("tech1"))


def test_only_one_pending_analysis_per_fault(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker(start=False)
    worker.request(fault, state.users.get("tech1"))
    with pytest.raises(RuntimeError, match="already pending"):
        worker.request(fault, state.users.get("admin"))


def test_the_queue_has_a_limit(state, make_fault, make_worker):
    worker = make_worker(start=False)
    for i in range(MAX_QUEUED_JOBS):
        worker.request(make_fault(f"F{i}"), state.users.get("tech1"))
    with pytest.raises(RuntimeError, match="queue is full"):
        worker.request(make_fault("One too many"), state.users.get("tech1"))


def test_cancelling_a_queued_job(state, make_fault, make_worker, db_path):
    fault = make_fault()
    worker = make_worker(start=False)
    worker.request(fault, state.users.get("tech1"))
    worker.cancel(fault, state.users.get("admin"))
    assert worker.status_of(fault.id)["state"] == "idle"
    assert fault.comments[-1].text == "admin requested to cancel Fake AI analysis."
    assert SqliteAnalysisJobStore(db_path).load_all() == []
    with pytest.raises(RuntimeError, match="no pending"):
        worker.cancel(fault, state.users.get("admin"))


def test_cancelling_the_running_job_discards_its_answer(state, make_fault, make_worker):
    provider = GateProvider()
    fault = make_fault()
    worker = make_worker(provider)
    worker.request(fault, state.users.get("tech1"))
    provider.started.wait(5)
    worker.cancel(fault, state.users.get("tech1"))
    assert worker.status_of(fault.id)["state"] == "idle"          # the buttons reset at once
    assert worker.queue_snapshot() == {"running": None, "queued": []}
    provider.gate.set()
    wait_until(lambda: provider.active == 0)
    time.sleep(0.1)
    assert ai_comments(fault) == []


def test_a_failure_becomes_an_error_comment(state, make_fault, make_worker, broadcaster):
    fault = make_fault()
    worker = make_worker(FakeProvider(error="timed out after 180 seconds"))
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert ai_comments(fault) == ["AI analysis by Fake AI (model: fake-model) failed: timed out after 180 seconds."]
    assert len(broadcaster.incident_events("ai_analysis_failed")) == 1


def test_an_unexpected_exception_does_not_kill_the_thread(state, make_fault, make_worker):
    class Broken(FakeProvider):
        def analyze(self, instructions, incident_data):
            raise KeyError("surprise")

    first, second = make_fault("First"), make_fault("Second")
    worker = make_worker(Broken())
    worker.request(first, state.users.get("tech1"))
    worker.request(second, state.users.get("tech1"))
    wait_for_analysis(worker, second)
    assert "unexpected error (KeyError)" in ai_comments(first)[0]
    assert worker.is_alive()


def test_a_long_answer_is_posted_as_parts_with_one_event(state, make_fault, make_worker, broadcaster):
    answer = "\n\n".join("Paragraph " + "z" * 2000 for _ in range(5))
    fault = make_fault()
    worker = make_worker(FakeProvider(answer=answer))
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    parts = ai_comments(fault)
    assert len(parts) == 3 and parts[0].startswith("AI analysis by Fake AI (model: fake-model) - Part 1/3")
    assert len(broadcaster.incident_events("ai_analysis_posted")) == 1


def test_queued_jobs_survive_a_restart(state, make_fault, make_worker, db_path, broadcaster):
    first, second = make_fault("First"), make_fault("Second")
    before = make_worker(start=False)
    before.request(first, state.users.get("tech1"))
    before.request(second, state.users.get("tech1"))

    provider = FakeProvider(model="another-model")        # a different provider/model after the restart
    after = AnalysisWorker(state, provider, SqliteAnalysisJobStore(db_path), broadcaster, 0)
    assert after.queue_snapshot() == {"running": None, "queued": [first.id, second.id]}
    after.start()
    try:
        wait_for_analysis(after, first, second)
        assert "(model: another-model)" in ai_comments(first)[0]
    finally:
        after.stop()


def test_only_one_worker_thread_can_run(make_worker):
    make_worker()
    second = make_worker(start=False)
    with pytest.raises(RuntimeError, match=THREAD_NAME):
        second.start()


def test_stop_ends_the_thread(make_worker):
    worker = make_worker()
    assert any(t.name == THREAD_NAME for t in threading.enumerate())
    worker.stop()
    assert not worker.is_alive()


def test_every_queue_change_publishes_the_queue(state, make_fault, make_worker, broadcaster):
    fault = make_fault()
    worker = make_worker(start=False)
    worker.request(fault, state.users.get("tech1"))
    assert broadcaster.events[-1] == {"scope": "ai_analysis", "running": None, "queued": [fault.id]}
    worker.cancel(fault, state.users.get("tech1"))
    assert broadcaster.events[-1] == {"scope": "ai_analysis", "running": None, "queued": []}


def test_a_reopened_fault_is_still_analyzed(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker(start=False)
    worker.request(fault, state.users.get("tech1"))
    fault.close(state.users.get("admin"), ResolutionType.RESOLVED, "Fixed.")
    fault.reopen(state.users.get("admin"), IncidentStatus.OPEN, "Came back.")
    worker.start()
    wait_for_analysis(worker, fault)


def test_a_busy_model_falls_back_and_the_header_names_the_model_that_answered(state, make_fault, make_worker):
    provider = FakeProvider(models=["best-model", "backup-model"], busy_models={"best-model"})
    fault = make_fault()
    worker = make_worker(provider)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert provider.asked == ["best-model", "backup-model"]
    assert ai_comments(fault)[0].startswith("AI analysis by Fake AI (model: backup-model)\n\n")


def test_when_every_model_is_busy_one_error_comment_lists_them(state, make_fault, make_worker, broadcaster):
    provider = FakeProvider(models=["a", "b"], busy_models={"a", "b"})
    fault = make_fault()
    worker = make_worker(provider)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert ai_comments(fault) == ["AI analysis by Fake AI (models: a > b) failed: all 2 models were unavailable - "
                                  "a is overloaded (503); b is overloaded (503)."]
    assert len(broadcaster.incident_events("ai_analysis_failed")) == 1


# ----------------------------------------------------------------------
# Request history (app/ai/request_history.py, task A1): every request() is
# recorded at once, and updated with how it ended.
# ----------------------------------------------------------------------

def test_a_request_is_recorded_as_success(state, make_fault, make_worker, request_history):
    fault = make_fault()
    worker = make_worker(request_history=request_history)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    wait_until(lambda: request_history.all_requests()[0]["outcome"] is not None)

    [row] = request_history.all_requests()
    assert row["incident_id"] == fault.id
    assert row["requested_by"] == "tech1"
    assert row["outcome"] == "success"
    assert row["provider"] == "Fake AI"
    assert row["model"] == "fake-model"
    assert row["finished_at"] is not None


def test_a_failure_is_recorded_as_failed(state, make_fault, make_worker, request_history):
    fault = make_fault()
    worker = make_worker(FakeProvider(error="boom"), request_history=request_history)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    wait_until(lambda: request_history.all_requests()[0]["outcome"] is not None)

    [row] = request_history.all_requests()
    assert row["outcome"] == "failed"
    assert row["model"] is None


def test_a_dropped_job_is_recorded_as_dropped(state, make_fault, make_worker, request_history):
    provider = GateProvider()
    provider.gate.set()
    closed = make_fault("Closed later")
    worker = make_worker(provider, start=False, request_history=request_history)
    worker.request(closed, state.users.get("tech1"))
    closed.close(state.users.get("tech1"), ResolutionType.RESOLVED, "Fixed meanwhile.")
    worker.start()
    wait_until(lambda: len(request_history.all_requests()) == 1 and request_history.all_requests()[0]["outcome"])

    [row] = request_history.all_requests()
    assert row["outcome"] == "dropped"


def test_cancelling_a_queued_job_is_recorded_as_cancelled(state, make_fault, make_worker, request_history):
    fault = make_fault()
    worker = make_worker(start=False, request_history=request_history)
    worker.request(fault, state.users.get("tech1"))
    worker.cancel(fault, state.users.get("admin"))

    [row] = request_history.all_requests()
    assert row["outcome"] == "cancelled"


def test_cancelling_the_running_job_is_recorded_as_cancelled(state, make_fault, make_worker, request_history):
    provider = GateProvider()
    fault = make_fault()
    worker = make_worker(provider, request_history=request_history)
    worker.request(fault, state.users.get("tech1"))
    provider.started.wait(5)
    worker.cancel(fault, state.users.get("tech1"))
    provider.gate.set()
    wait_until(lambda: request_history.all_requests()[0]["outcome"] is not None)

    [row] = request_history.all_requests()
    assert row["outcome"] == "cancelled"


def test_no_request_history_store_means_recording_is_skipped(state, make_fault, make_worker):
    """The default (request_history=None) - every existing test uses this - still works end to end."""
    fault = make_fault()
    worker = make_worker()
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert ai_comments(fault) != []
