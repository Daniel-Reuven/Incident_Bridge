"""
Tests for the re-analysis rule (app/ai/reanalysis.py), its stored history
(SqliteAnalysisJobStore.record_success / last_success) and how the worker
applies it (AnalysisWorker.request / reanalysis_status in app/ai/worker.py).

Run from backend/:  python -m pytest tests/test_ai/test_reanalysis.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers.fake_provider import FakeProvider
from app.ai.reanalysis import LastAnalysis, analysis_fingerprint, decide, wait_text
from app.ai.worker import AnalysisWorker
from app.models import IncidentStatus, ResolutionType, SeverityCategory
from app.models.system_users import AI_USERNAME

from tests.test_ai.conftest import wait_for_analysis

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def last(provider="Gemini", fingerprint="abc", minutes_ago=2):
    return LastAnalysis(provider, "gemini-3.8-flash", fingerprint, NOW - timedelta(minutes=minutes_ago))


# --- decide() --------------------------------------------------------------------------

def test_a_fault_never_analyzed_is_allowed():
    assert decide(None, "abc", "Gemini", False, 300, NOW).allowed


@pytest.mark.parametrize("fingerprint, provider", [("changed", "Gemini"), ("abc", "Claude")])
def test_a_change_or_another_provider_allows_it_again(fingerprint, provider):
    assert decide(last(), fingerprint, provider, False, 300, NOW).allowed


def test_no_changes_same_provider_is_refused_for_a_user():
    decision = decide(last(), "abc", "Gemini", False, 300, NOW)
    assert not decision.allowed and not decision.can_force
    assert decision.reason == ("Gemini already analyzed this fault 2 minutes ago and nothing has changed since. "
                               "Add an update first, or ask an admin to run it again.")


def test_an_admin_waits_for_the_cooldown_then_may_force():
    within = decide(last(minutes_ago=2), "abc", "Gemini", True, 300, NOW)
    assert (within.allowed, within.can_force, within.force_wait_seconds) == (False, False, 180)
    after = decide(last(minutes_ago=6), "abc", "Gemini", True, 300, NOW)
    assert (after.allowed, after.can_force, after.force_wait_seconds) == (False, True, 0)


def test_wait_text():
    assert wait_text(192) == "3:12" and wait_text(5) == "0:05"


# --- the fingerprint ----------------------------------------------------------------------

def test_the_ais_own_answers_and_the_bookkeeping_comments_are_not_changes(state, make_fault):
    fault = make_fault()
    before = analysis_fingerprint(fault, AI_USERNAME)
    fault.add_comment(state.users.get("tech1"), "tech1 requested Gemini analysis.")
    fault.add_comment(state.users.get(AI_USERNAME), "AI analysis by Gemini (model: x)\n\nSummary: ...")
    fault.add_comment(state.users.get("admin"), "admin requested to cancel Gemini analysis.")
    fault.add_comment(state.users.get("admin"), "admin requested Gemini analysis again (forced).")
    assert analysis_fingerprint(fault, AI_USERNAME) == before


@pytest.mark.parametrize("change", ["comment", "claim", "severity", "close-reopen"])
def test_user_updates_are_changes(state, make_fault, change):
    fault = make_fault()
    before = analysis_fingerprint(fault, AI_USERNAME)
    admin = state.users.get("admin")
    if change == "comment":
        fault.add_comment(state.users.get("tech1"), "Restarted the gateway, no effect.")
    elif change == "claim":
        fault.claim(state.users.get("tech1"))
    elif change == "severity":
        fault.severity = SeverityCategory.CRITICAL
    else:
        fault.close(admin, ResolutionType.RESOLVED, "Fixed.")
        fault.reopen(admin, IncidentStatus.OPEN, "Came back.")
    assert analysis_fingerprint(fault, AI_USERNAME) != before


# --- the stored history ----------------------------------------------------------------------

def test_the_last_success_survives_reopening_the_database(tmp_path):
    path = str(tmp_path / "history.db")
    SqliteAnalysisJobStore(path).record_success("f-1", last(minutes_ago=0))
    SqliteAnalysisJobStore(path).record_success("f-1", last(provider="Claude", minutes_ago=0))   # replaces
    assert SqliteAnalysisJobStore(path).last_success("f-1").provider == "Claude"
    assert SqliteAnalysisJobStore(path).last_success("f-2") is None


# --- the worker ----------------------------------------------------------------------------

def ai_comments(fault):
    return [c for c in fault.comments if c.author.username == AI_USERNAME]


def analyze_once(state, worker, fault, user="tech1"):
    worker.request(fault, state.users.get(user))
    wait_for_analysis(worker, fault)


def test_a_second_request_without_changes_is_refused(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker()
    analyze_once(state, worker, fault)
    with pytest.raises(RuntimeError, match="already analyzed this fault just now and nothing has changed"):
        worker.request(fault, state.users.get("tech1"))
    status = worker.reanalysis_status(fault, state.users.get("tech1"))
    assert status["blocked"] and status["last_analysis"]["provider"] == "Fake AI"


def test_a_user_update_allows_it_again(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker()
    analyze_once(state, worker, fault)
    fault.add_comment(state.users.get("admin"), "New finding: it only fails on mobile.")
    worker.request(fault, state.users.get("tech1"))            # no error


def test_another_provider_allows_it_again(state, make_fault, make_worker, db_path, broadcaster):
    fault = make_fault()
    analyze_once(state, make_worker(), fault)
    other = FakeProvider()
    other.display_name = "Other AI"                             # e.g. Gemini then, Claude now
    after_restart = AnalysisWorker(state, other, SqliteAnalysisJobStore(db_path), broadcaster, 0,
                                   start_on_request=False)
    after_restart.request(fault, state.users.get("tech1"))      # no error: a different provider


def test_failed_or_cancelled_analyses_do_not_count(state, make_fault, make_worker):
    failed, cancelled = make_fault("Failed"), make_fault("Cancelled")
    worker = make_worker(FakeProvider(error="timed out after 180 seconds"))
    analyze_once(state, worker, failed)
    worker.request(failed, state.users.get("tech1"))            # allowed: the first one failed
    worker.cancel(failed, state.users.get("tech1"))
    worker.request(cancelled, state.users.get("tech1"))
    worker.cancel(cancelled, state.users.get("tech1"))
    worker.request(cancelled, state.users.get("tech1"))         # allowed: it never answered


def test_an_admin_can_force_only_after_the_cooldown(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker(cooldown_seconds=300)
    analyze_once(state, worker, fault)
    admin = state.users.get("admin")
    with pytest.raises(RuntimeError, match=r"Use Re-analyze to run it again\."):
        worker.request(fault, admin)
    with pytest.raises(RuntimeError, match=r"It can be run again in \d:\d\d \(5-minute cooldown\)"):
        worker.request(fault, admin, force=True)
    assert worker.reanalysis_status(fault, admin)["force_wait_seconds"] > 290

    worker.cooldown_seconds = 0                                  # the cooldown has passed
    assert worker.reanalysis_status(fault, admin)["can_force"]
    worker.request(fault, admin, force=True)
    assert fault.comments[-1].text == "admin requested Fake AI analysis again (forced)."


def test_only_an_admin_can_force(state, make_fault, make_worker):
    fault = make_fault()
    worker = make_worker(cooldown_seconds=0)
    analyze_once(state, worker, fault)
    with pytest.raises(PermissionError, match="Only an admin"):
        worker.request(fault, state.users.get("tech1"), force=True)
