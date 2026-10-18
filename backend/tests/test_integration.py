"""
End-to-end tests for the AI analysis -> site impact pipeline (brief 6.4):
a fault's AI analysis, its structured findings (app/ai/result.py), the
SiteImpactListener that turns an affected site id into a REAL availability
check (app/analysis_service.py), and the permanent request history that
outcome ends up in (app/ai/request_history.py). No network: FakeProvider
answers with any text the test gives it, FakeChecker answers with scripted
HTTP results.

Run from backend/:  python -m pytest tests/test_integration.py -v
"""

import time

import pytest

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.providers.fake_provider import FakeProvider
from app.ai.request_history import OUTCOME_FAILED, OUTCOME_SUCCESS
from app.ai.worker import AnalysisWorker
from app.analysis_service import AnalysisStatsListener, SiteImpactListener
from app.events import EventBroadcaster
from app.models import Fault, NotificationState, SeverityCategory, SiteStatus
from app.models.system_users import AI_USERNAME
from app.reports import build_site_report
from app.services.availability import FakeChecker
from app.services.notifier import OutboxNotifier
from app.sites import SiteDirectory
from app.state import AppState

SITE_URL = "https://s1007.example.com"


def answer_naming(site_id, body="Summary:\n- thing happened\n\nSeverity assessment:\n- looks right"):
    """A FakeProvider answer whose trailing findings block names `site_id` as affected (or none, for None)."""
    ids = [] if site_id is None else [site_id]
    return f'{body}\n\n{{"suggested_severity": "major", "affected_site_ids": {ids}, "confidence": 0.9}}'


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "ai.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.delenv("INCIDENT_BRIDGE_USERS", raising=False)
    return path


@pytest.fixture
def make_state(db_path):
    """A real AppState whose SiteDirectory uses the given FakeChecker (no network) instead of an HttpChecker."""
    def make(checker: FakeChecker) -> AppState:
        state = AppState.create()
        state.sites = SiteDirectory(checker=checker, notifier=OutboxNotifier())
        return state
    return make


@pytest.fixture
def ai_log(caplog):
    """The AI console log (app/ai/log.py) written during the test."""
    from app.ai.log import log
    log.addHandler(caplog.handler)
    caplog.set_level("DEBUG", logger=log.name)
    yield caplog
    log.removeHandler(caplog.handler)


def make_worker(state, provider, db_path) -> AnalysisWorker:
    stats = AnalysisStatsListener()
    worker = AnalysisWorker(state, provider, SqliteAnalysisJobStore(db_path), EventBroadcaster(), 0.0,
                            listeners=[SiteImpactListener(state.sites), stats])
    worker.stats = stats
    worker.start()
    return worker


def make_fault(state, title="Checkout failing") -> Fault:
    fault = Fault(title=title, description="Payments fail for some users.", created_by=state.users.get("tech1"),
                  severity=SeverityCategory.MAJOR, severity_score=2.0, details={})
    state.incidents.add(fault)
    return fault


def wait_idle(worker, fault, timeout=5.0):
    deadline = time.monotonic() + timeout
    while worker.status_of(fault.id)["state"] != "idle" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert worker.status_of(fault.id)["state"] == "idle", "analysis did not finish in time"


# --- 1. full flow succeeds -------------------------------------------------

def test_full_flow_check_runs_and_the_report_reflects_it(make_state, db_path):
    checker = FakeChecker({SITE_URL: [503]})            # scripted: first check is a 5xx
    state = make_state(checker)
    admin = state.users.get("admin")
    site = state.sites.create_site(admin, "Checkout", SITE_URL)
    state.sites.create_mailing_list(admin, "ops", "Ops", ["ops@example.com"], [site.site_id])

    provider = FakeProvider(answer=answer_naming(site.site_id))
    worker = make_worker(state, provider, db_path)
    fault = make_fault(state, title=f"Site {site.site_id} checkout failing")
    worker.request(fault, state.users.get("tech1"))
    wait_idle(worker, fault)
    worker.stop()

    # The check actually ran, against the site's own URL.
    assert checker.calls == [SITE_URL]
    # The measured check decided the status - Unknown -> Degraded on a first 5xx (app/models/site.py).
    assert site.status == SiteStatus.DEGRADED
    # Worth notifying (Unknown -> Operational is the only exempt case) - a Draft, not auto-sent.
    drafts = [n for n in state.sites.notifications() if n.site_id == site.site_id]
    assert len(drafts) == 1 and drafts[0].state == NotificationState.DRAFT

    assert worker.history.outcomes_by_incident()[fault.id] == [OUTCOME_SUCCESS]

    report = build_site_report(state.sites, state.incidents.list_all(), worker.history)
    row = {r.site_id: r for r in report.rows}[site.site_id]
    assert (row.ai_analyses_requested, row.ai_analyses_succeeded, row.ai_analyses_failed) == (1, 1, 0)
    # Degraded (not Operational) and only a Draft (not sent) - still flagged for a decision.
    assert site.site_id in report.ai_resolved_or_unnotified_sites


# --- 2. service failure -----------------------------------------------------

def test_a_provider_failure_never_touches_a_site(make_state, db_path):
    checker = FakeChecker()
    state = make_state(checker)
    admin = state.users.get("admin")
    site = state.sites.create_site(admin, "Checkout", SITE_URL)

    provider = FakeProvider(error="the model is unreachable")
    worker = make_worker(state, provider, db_path)
    fault = make_fault(state)
    worker.request(fault, state.users.get("tech1"))
    wait_idle(worker, fault)
    worker.stop()

    ai_comments = [c.text for c in fault.comments if c.author.username == AI_USERNAME]
    assert len(ai_comments) == 1 and "failed" in ai_comments[0].lower()
    assert checker.calls == []                      # no findings were ever posted - no check ran
    assert site.status == SiteStatus.UNKNOWN         # nothing about the site changed
    assert list(state.sites.notifications()) == []
    assert worker.history.outcomes_by_incident()[fault.id] == [OUTCOME_FAILED]


# --- 3. invalid cross-references -------------------------------------------

def test_an_unknown_site_id_is_an_invalid_cross_reference(make_state, db_path, ai_log):
    checker = FakeChecker()
    state = make_state(checker)
    provider = FakeProvider(answer=answer_naming(999999))   # no such site
    worker = make_worker(state, provider, db_path)
    fault = make_fault(state)
    worker.request(fault, state.users.get("tech1"))
    wait_idle(worker, fault)
    worker.stop()

    assert checker.calls == []
    assert list(state.sites.notifications()) == []
    assert "does not exist" in ai_log.text
    # The analysis itself still succeeded - it is the site reference that is bad, not the AI call.
    assert worker.history.outcomes_by_incident()[fault.id] == [OUTCOME_SUCCESS]


def test_an_archived_site_id_is_an_invalid_cross_reference(make_state, db_path, ai_log):
    checker = FakeChecker()
    state = make_state(checker)
    admin = state.users.get("admin")
    site = state.sites.create_site(admin, "Old", SITE_URL)
    state.sites.archive_site(site.site_id, admin)

    provider = FakeProvider(answer=answer_naming(site.site_id))
    worker = make_worker(state, provider, db_path)
    fault = make_fault(state)
    worker.request(fault, state.users.get("tech1"))
    wait_idle(worker, fault)
    worker.stop()

    assert checker.calls == []                 # archived sites are never checked
    assert site.is_archived                      # unchanged
    assert list(state.sites.notifications()) == []
    assert "archived" in ai_log.text
    assert worker.history.outcomes_by_incident()[fault.id] == [OUTCOME_SUCCESS]
