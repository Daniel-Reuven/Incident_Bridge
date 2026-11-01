"""
Tests for the AI analysis console log (app/ai/log.py and the log lines in
app/ai/worker.py and app/ai/providers/base.py).

Run from backend/:  python -m pytest tests/test_ai/test_logging.py -v
"""

import logging

import pytest

from app.ai.log import apply_log_level, log, short_title
from app.ai.providers.fake_provider import FakeProvider
from app.ai.setup import check_ai_settings
from app.models.system_users import AI_USERNAME

from tests.test_ai.conftest import wait_until


def lines_containing(ai_log, text):
    return [m for m in ai_log.messages if text in m]


def test_one_job_is_logged_from_queue_to_finish_with_the_queue_state(state, make_fault, make_worker, ai_log):
    provider = FakeProvider(models=["best", "backup"], busy_models={"best"})
    fault = make_fault("Checkout failing")
    worker = make_worker(provider, start=False)
    worker.request(fault, state.users.get("tech1"))
    worker.start()
    wait_until(lambda: lines_containing(ai_log, "Finished fault"))

    messages = ai_log.messages
    assert any(m.startswith(f'Queued fault {fault.id} "Checkout failing" for tech1 - position 1 | '
                            f'queue: idle, 1 waiting ({fault.id})') for m in messages)
    assert any(m.startswith("Worker thread ai-analysis-worker started") for m in messages)
    assert any(m.startswith(f'Started fault {fault.id} "Checkout failing" (requested by tech1') for m in messages)
    assert "Asking Fake AI best (model 1 of 2)" in messages
    assert any(m.startswith("best is overloaded (503) after") and m.endswith("- trying backup") for m in messages)
    assert any(m.startswith("Answered by backup in") for m in messages)
    finished = lines_containing(ai_log, "Finished fault")[0]
    assert f"Finished fault {fault.id}: posted 1 comment" in finished and finished.endswith("| queue: idle, 0 waiting")


def test_busy_models_are_warnings_and_steps_are_info(state, make_fault, make_worker, ai_log):
    fault = make_fault()
    worker = make_worker(FakeProvider(models=["a", "b"], busy_models={"a"}))
    worker.request(fault, state.users.get("tech1"))
    wait_until(lambda: lines_containing(ai_log, "Finished fault"))
    levels = {record.getMessage().split(" ")[0]: record.levelname for record in ai_log.records}
    assert levels["a"] == "WARNING" and levels["Queued"] == "INFO" and levels["Finished"] == "INFO"


def test_refusals_cancels_and_drops_are_logged(state, make_fault, make_worker, ai_log):
    from app.models import ResolutionType
    fault, closed_later = make_fault("First"), make_fault("Second")
    worker = make_worker(start=False)
    worker.request(fault, state.users.get("tech1"))
    with pytest.raises(RuntimeError):
        worker.request(fault, state.users.get("admin"))
    worker.cancel(fault, state.users.get("admin"))
    worker.request(closed_later, state.users.get("tech1"))
    closed_later.close(state.users.get("tech1"), ResolutionType.RESOLVED, "Fixed meanwhile.")
    worker.start()
    wait_until(lambda: lines_containing(ai_log, "Finished fault"))

    assert f"Refused a request for fault {fault.id} by admin - an analysis is already pending" in ai_log.messages
    assert any(m.startswith(f"Cancelled fault {fault.id} by admin - it was waiting at position 1") for m in ai_log.messages)
    assert f"Dropped the job for fault {closed_later.id} - it is closed or no longer exists" in ai_log.messages
    assert any(f"Finished fault {closed_later.id}: dropped (closed or gone)" in m for m in ai_log.messages)


def test_cancelling_the_running_job_logs_that_its_answer_is_discarded(state, make_fault, make_worker, ai_log):
    fault = make_fault()
    worker = make_worker(FakeProvider(delay_seconds=0.3))
    worker.request(fault, state.users.get("tech1"))
    wait_until(lambda: lines_containing(ai_log, "Asking"))
    worker.cancel(fault, state.users.get("tech1"))
    wait_until(lambda: lines_containing(ai_log, "Finished fault"))
    assert lines_containing(ai_log, "it was running - the call will finish and its answer will be discarded")
    assert lines_containing(ai_log, "answer discarded (cancelled while running)")
    assert not any(c.author.username == AI_USERNAME for c in fault.comments)


def test_the_pause_is_logged_at_debug_level(state, make_fault, make_worker, ai_log):
    fault = make_fault()
    worker = make_worker(delay_seconds=0.05)
    worker.request(fault, state.users.get("tech1"))
    wait_until(lambda: lines_containing(ai_log, "Pausing"))
    assert [r.levelname for r in ai_log.records if r.getMessage().startswith("Pausing")] == ["DEBUG"]


def test_restored_jobs_are_logged(state, make_fault, make_worker, ai_log):
    fault = make_fault()
    make_worker(start=False).request(fault, state.users.get("tech1"))
    make_worker(start=False)                                   # a "restart" reading the same database
    assert f"Restored 1 queued job(s) from the database: {fault.id}" in ai_log.messages


def test_queue_summary_lists_at_most_five_waiting_ids(state, make_fault, make_worker):
    worker = make_worker(start=False)
    faults = [make_fault(f"F{i}") for i in range(7)]
    for fault in faults:
        worker.request(fault, state.users.get("tech1"))
    summary = worker.queue_summary()
    assert summary.startswith("queue: idle, 7 waiting (") and summary.endswith(", …)")
    assert faults[4].id in summary and faults[5].id not in summary


def test_short_titles_keep_lines_short():
    assert short_title("Short") == '"Short"'
    assert short_title("x" * 60) == '"' + "x" * 39 + '…"'


@pytest.mark.parametrize("value, level", [("debug", logging.DEBUG), ("WARNING", logging.WARNING),
                                          ("off", logging.CRITICAL + 1)])
def test_ai_log_level_setting(monkeypatch, value, level):
    monkeypatch.setenv("AI_LOG_LEVEL", value)
    apply_log_level()
    assert log.level == level
    monkeypatch.delenv("AI_LOG_LEVEL")
    apply_log_level()
    assert log.level == logging.INFO


def test_a_bad_log_level_stops_startup(monkeypatch):
    monkeypatch.setenv("AI_LOG_LEVEL", "LOUD")
    with pytest.raises(ValueError, match="AI_LOG_LEVEL='LOUD' is not supported"):
        check_ai_settings()
