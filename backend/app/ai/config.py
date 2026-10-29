"""
AI analysis settings: those saved on the setup page (encrypted in the database),
else the environment (backend/.env) - see app/ai/stored_settings.py.

    AI_PROVIDER=gemini                 # gemini (default) | claude | openai | fake | off
    GEMINI_API_KEY=...    GEMINI_TIER=free | paid    (optional: GEMINI_MODELS=a,b,c)
    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=...
    OPENAI_API_KEY=...    OPENAI_MODEL=...
    AI_REQUEST_TIMEOUT_SECONDS=180     # per API call, every provider
    AI_REQUEST_DELAY_SECONDS=5         # pause after each API call, every provider
    AI_REANALYSIS_COOLDOWN_SECONDS=300 # before an admin may force a re-analysis (app/ai/reanalysis.py)

Only ONE provider is active at a time. Only the selected provider's key and
model(s) are read. A model setting holds one model or a comma-separated list,
best first; the provider falls back along the list when a model is busy or
out of quota (app/ai/providers/base.py). Gemini needs no model setting: its
list comes from GEMINI_TIER (app/ai/providers/gemini_models.py). The feature is OFF (no worker, no Analyze button)
while the selected provider's API key is not set - so a fresh checkout with
no keys simply runs without it. AI_PROVIDER=fake needs no key (see
app/ai/providers/fake_provider.py).

AIConfig.from_env() raises ValueError, with a clear message, for settings
that are present but wrong (an unknown provider, a key without a model, an
unknown GEMINI_TIER or GEMINI_MODEL_DISCOVERY, a timeout that is not a
positive number). serve_app.py checks this before
starting the server, and the app checks it again at startup.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

from app.ai.providers import DEFAULT_PROVIDER, PROVIDERS
from app.ai.stored_settings import setting

DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_DELAY_SECONDS = 5.0
DEFAULT_COOLDOWN_SECONDS = 300.0


@dataclass(frozen=True)
class AIConfig:
    provider: str               # a key of PROVIDERS, e.g. "gemini"
    api_key: Optional[str]
    models: Tuple[str, ...]     # best first (the fake provider: its one fixed model)
    timeout_seconds: float
    delay_seconds: float
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS   # re-analysis cooldown for admins
    extra: dict = field(default_factory=dict)   # provider-specific settings (Gemini: tier, discovery)

    @classmethod
    def from_env(cls) -> Optional["AIConfig"]:
        """The configured settings, or None if the feature is off (the selected provider has no API key)."""
        provider = (setting("AI_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        if provider == "off":                             # turned off on purpose (e.g. the Server settings page)
            return None
        if provider not in PROVIDERS:
            raise ValueError(f"AI_PROVIDER={provider!r} is not supported - use one of: {', '.join(PROVIDERS)}.")
        info = PROVIDERS[provider]

        timeout = _read_number("AI_REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, allow_zero=False)
        delay = _read_number("AI_REQUEST_DELAY_SECONDS", DEFAULT_DELAY_SECONDS, allow_zero=True)
        cooldown = _read_number("AI_REANALYSIS_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS, allow_zero=True)

        api_key = _read_text(info.key_env) if info.key_env else None
        if info.key_env and not api_key:
            return None                                   # feature off
        models = _read_models(info)
        extra = info.extra_settings() if info.extra_settings else {}
        return cls(provider=provider, api_key=api_key, models=models, timeout_seconds=timeout,
                   delay_seconds=delay, cooldown_seconds=cooldown, extra=extra)

    @staticmethod
    def off_reason() -> str:
        """One line saying why the feature is off - printed at startup when from_env() returns None."""
        provider = (setting("AI_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
        if provider == "off":
            return "AI analysis is off (AI_PROVIDER=off)."
        info = PROVIDERS.get(provider)
        key = info.key_env if info else "the API key"
        return f"AI analysis is off: AI_PROVIDER={provider}, but {key} is not set."


def _read_models(info) -> Tuple[str, ...]:
    """
    The provider's models, best first: its model setting split on commas,
    else its default_models() (Gemini: the tier's list). Raises ValueError
    if neither gives a model.
    """
    if info.models_env is None:                    # a fixed model (the fake provider)
        return tuple(info.default_models()) if info.default_models else ()
    raw = _read_text(info.models_env) or ""
    models = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not models and info.default_models is not None:
        models = info.default_models()
    if not models:
        raise ValueError(f"{info.key_env} is set, so AI analysis is on - set {info.models_env} to the model to use.")
    return models


def _read_text(name: str) -> Optional[str]:
    value = setting(name)
    return value.strip() if value and value.strip() else None


def _read_number(name: str, default: float, allow_zero: bool) -> float:
    raw = _read_text(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0 or (value == 0 and not allow_zero):
        kind = "a number, 0 or more" if allow_zero else "a positive number"
        raise ValueError(f"{name} must be {kind} of seconds (got {raw!r}).")
    return value
