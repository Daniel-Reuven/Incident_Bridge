"""
Tests for Gemini model discovery (GEMINI_MODEL_DISCOVERY=auto):
refresh_model_list() and gemini_settings() in app/ai/providers/gemini_models.py,
and the worker thread refreshing the list once before its first job
(AnalysisWorker._refresh_models in app/ai/worker.py). No network - the
listing through the REST API (stub server) is tested in test_providers.py.

Run from backend/:  python -m pytest tests/test_ai/test_model_discovery.py -v
"""

import pytest

from app.ai.config import AIConfig
from app.ai.providers.fake_provider import FakeProvider
from app.ai.providers.gemini_models import FREE_TIER_MODELS, PAID_TIER_MODELS, refresh_model_list

from tests.test_ai.conftest import wait_for_analysis

# What Google might list one day: a newer flash and flash-lite, a newer pro,
# a preview, an older model, a non-text model - and gemini-3.1-flash-lite retired.
LISTED = ["gemini-3.9-flash", "gemini-3.9-flash-lite", "gemini-4-pro", "gemini-4-flash-preview",
          "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
          "gemini-2.5-flash", "text-embedding-005"]


def test_free_tier_adds_newer_flash_models_first_and_drops_retired_ones():
    result = refresh_model_list(LISTED, FREE_TIER_MODELS, "free")
    assert result.added == ["gemini-3.9-flash", "gemini-3.9-flash-lite"]
    assert result.removed == ["gemini-3.1-flash-lite"]
    assert result.models == ["gemini-3.9-flash", "gemini-3.9-flash-lite",
                             "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_free_tier_never_adds_pro_preview_or_older_models():
    added = refresh_model_list(LISTED, FREE_TIER_MODELS, "free").added
    assert "gemini-4-pro" not in added                 # paid family
    assert "gemini-4-flash-preview" not in added       # preview
    assert "gemini-2.5-flash" not in added             # older than the list's newest flash


def test_paid_tier_also_adds_a_newer_pro_model_in_front():
    result = refresh_model_list(LISTED, PAID_TIER_MODELS, "paid")
    assert result.added == ["gemini-4-pro", "gemini-3.9-flash", "gemini-3.9-flash-lite"]
    assert result.models[:4] == ["gemini-4-pro", "gemini-3.9-flash", "gemini-3.9-flash-lite", "gemini-3.1-pro-preview"]


def test_a_list_set_by_hand_is_only_checked_never_extended():
    result = refresh_model_list(LISTED, ["gemini-3.6-flash", "gemini-0-gone"], "free", extend=False)
    assert result.added == [] and result.removed == ["gemini-0-gone"]
    assert result.models == ["gemini-3.6-flash"]


def test_if_nothing_would_be_left_the_configured_list_is_kept():
    result = refresh_model_list(["text-embedding-005"], ["gemini-0-gone"], "free", extend=False)
    assert result.models == ["gemini-0-gone"] and result.removed == []


def test_the_summary_line_says_what_changed():
    summary = refresh_model_list(LISTED, FREE_TIER_MODELS, "free").summary()
    assert summary.startswith("Gemini model list refreshed - added gemini-3.9-flash, gemini-3.9-flash-lite; "
                              "removed gemini-3.1-flash-lite (no longer offered).")


# --- settings ---------------------------------------------------------------------

@pytest.fixture
def gemini_env(monkeypatch):
    for name in ("AI_PROVIDER", "GEMINI_MODEL", "GEMINI_MODELS", "GEMINI_TIER", "GEMINI_MODEL_DISCOVERY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    return monkeypatch


def test_discovery_is_off_unless_asked_for(gemini_env):
    assert AIConfig.from_env().extra == {"tier": "free", "discovery": False, "extend": True}
    gemini_env.setenv("GEMINI_MODEL_DISCOVERY", "AUTO")
    gemini_env.setenv("GEMINI_TIER", "paid")
    assert AIConfig.from_env().extra == {"tier": "paid", "discovery": True, "extend": True}


def test_a_hand_set_list_turns_extending_off(gemini_env):
    gemini_env.setenv("GEMINI_MODEL_DISCOVERY", "auto")
    gemini_env.setenv("GEMINI_MODELS", "gemini-3.6-flash")
    assert AIConfig.from_env().extra["extend"] is False


def test_an_unknown_discovery_mode_stops_startup(gemini_env):
    gemini_env.setenv("GEMINI_MODEL_DISCOVERY", "sometimes")
    with pytest.raises(ValueError, match="GEMINI_MODEL_DISCOVERY='sometimes' is not supported"):
        AIConfig.from_env()


# --- the worker refreshes once, before its first job ----------------------------------

class RefreshingProvider(FakeProvider):
    """A fake that 'discovers' a newer model (or fails to) when the worker starts."""

    def __init__(self, fail=False, **kwargs):
        super().__init__(models=["old-model"], **kwargs)
        self.fail = fail
        self.refreshed = 0

    def refresh_models(self):
        self.refreshed += 1
        if self.fail:
            raise ConnectionError("network down")
        self.models = ["new-model", "old-model"]
        return "list refreshed"


def test_the_worker_refreshes_the_list_before_its_first_job(state, make_fault, make_worker, ai_log):
    provider = RefreshingProvider()
    fault = make_fault()
    worker = make_worker(provider)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert provider.refreshed == 1 and provider.asked == ["new-model"]
    assert "list refreshed" in ai_log.messages


def test_a_failed_refresh_keeps_the_list_and_the_worker_running(state, make_fault, make_worker, ai_log):
    provider = RefreshingProvider(fail=True)
    fault = make_fault()
    worker = make_worker(provider)
    worker.request(fault, state.users.get("tech1"))
    wait_for_analysis(worker, fault)
    assert provider.asked == ["old-model"] and worker.is_alive()
    assert any("Could not refresh the model list (ConnectionError: network down)" in m for m in ai_log.messages)
