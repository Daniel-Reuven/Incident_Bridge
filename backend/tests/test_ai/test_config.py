"""
Tests for the AI settings (app/ai/config.py) and the provider registry
(app/ai/providers/__init__.py).

Run from backend/:  python -m pytest tests/test_ai/test_config.py -v
"""

import importlib

import pytest

from app.ai.config import AIConfig
from app.ai.providers import DEFAULT_PROVIDER, PROVIDERS, build_provider
from app.ai.providers.fake_provider import FakeProvider
from app.ai.providers.gemini_models import FREE_TIER_MODELS, PAID_TIER_MODELS
from app.ai.setup import check_ai_settings

AI_VARIABLES = ["AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_MODELS", "GEMINI_TIER",
                "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL",
                "AI_REQUEST_TIMEOUT_SECONDS", "AI_REQUEST_DELAY_SECONDS", "AI_REANALYSIS_COOLDOWN_SECONDS"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in AI_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_gemini_is_the_default_provider():
    assert DEFAULT_PROVIDER == "gemini"


def test_the_feature_is_off_without_an_api_key():
    assert AIConfig.from_env() is None
    assert "GEMINI_API_KEY is not set" in AIConfig.off_reason()


@pytest.mark.parametrize("provider, key, models_env", [
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODELS"),
    ("claude", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    ("openai", "OPENAI_API_KEY", "OPENAI_MODEL"),
])
def test_each_provider_reads_its_own_key_and_model_list(monkeypatch, provider, key, models_env):
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv(key, "secret")
    monkeypatch.setenv(models_env, " best-model, backup-model ,")    # a comma-separated list, best first
    config = AIConfig.from_env()
    assert (config.provider, config.api_key, config.models) == (provider, "secret", ("best-model", "backup-model"))
    assert (config.timeout_seconds, config.delay_seconds) == (180.0, 5.0)


def test_gemini_needs_no_model_setting_and_defaults_to_the_free_tier(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    assert AIConfig.from_env().models == FREE_TIER_MODELS


def test_a_paid_gemini_key_tries_the_paid_model_first(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_TIER", "Paid")
    models = AIConfig.from_env().models
    assert models == PAID_TIER_MODELS
    assert models[1:] == FREE_TIER_MODELS              # then falls back to the free models


def test_gemini_models_override_the_tier_and_the_old_single_setting_still_works(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_TIER", "paid")
    monkeypatch.setenv("GEMINI_MODEL", "old-single")
    assert AIConfig.from_env().models == ("old-single",)
    monkeypatch.setenv("GEMINI_MODELS", "a,b")
    assert AIConfig.from_env().models == ("a", "b")


def test_another_providers_key_does_not_turn_the_feature_on(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "claude")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    assert AIConfig.from_env() is None


def test_timeout_and_delay_are_shared_settings(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "fake")
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("AI_REQUEST_DELAY_SECONDS", "0")
    config = AIConfig.from_env()
    assert (config.timeout_seconds, config.delay_seconds) == (60.0, 0.0)


@pytest.mark.parametrize("env, message", [
    ({"AI_PROVIDER": "copilot"}, "not supported"),
    ({"AI_PROVIDER": "claude", "ANTHROPIC_API_KEY": "secret"}, "set ANTHROPIC_MODEL"),
    ({"GEMINI_API_KEY": "secret", "GEMINI_TIER": "gold"}, "GEMINI_TIER='gold' is not supported"),
    ({"AI_PROVIDER": "fake", "AI_REQUEST_TIMEOUT_SECONDS": "0"}, "positive number"),
    ({"AI_PROVIDER": "fake", "AI_REQUEST_TIMEOUT_SECONDS": "soon"}, "positive number"),
    ({"AI_PROVIDER": "fake", "AI_REQUEST_DELAY_SECONDS": "-1"}, "0 or more"),
    ({"AI_PROVIDER": "fake", "AI_REANALYSIS_COOLDOWN_SECONDS": "soon"}, "AI_REANALYSIS_COOLDOWN_SECONDS must be"),
])
def test_invalid_settings_raise_a_clear_error(monkeypatch, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        AIConfig.from_env()


def test_the_fake_provider_needs_no_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "Fake")       # case-insensitive
    provider = build_provider(AIConfig.from_env())
    assert isinstance(provider, FakeProvider)
    assert provider.label() == "Fake AI (model: fake-model)"


def test_a_missing_sdk_gives_the_pip_command(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    real_import = importlib.import_module

    def failing_import(name, *args):
        if name.endswith("gemini_provider"):
            raise ModuleNotFoundError("No module named 'google'", name="google")
        return real_import(name, *args)

    monkeypatch.setattr(importlib, "import_module", failing_import)
    with pytest.raises(ValueError, match=r'pip install -e "\.\[gemini\]"'):
        check_ai_settings()


def test_every_real_provider_has_its_own_pip_extra():
    for name, info in PROVIDERS.items():
        if name != "fake":
            assert info.pip_extra == name and info.key_env and info.models_env


def test_the_reanalysis_cooldown_defaults_to_five_minutes(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "fake")
    assert AIConfig.from_env().cooldown_seconds == 300.0
    monkeypatch.setenv("AI_REANALYSIS_COOLDOWN_SECONDS", "60")
    assert AIConfig.from_env().cooldown_seconds == 60.0
