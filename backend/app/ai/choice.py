"""
An AI choice made on a page - the setup page (app/api/setup.py) or the
Server settings page (app/api/server.py) - turned into the .env-style
values app/ai/config.py reads (kept encrypted in the settings, "ai"), and
checked with a real call to the provider.
"""

from typing import Optional

from app.ai import stored_settings
from app.ai.config import AIConfig
from app.ai.providers import PROVIDERS, build_provider
from app.api_client import ApiError


def provider_list() -> list:
    return [{"id": key, "name": info.display_name, "needs_key": bool(info.key_env),
             "models_setting": info.models_env} for key, info in PROVIDERS.items()]


def ai_values(provider: Optional[str], api_key: Optional[str] = None, models: Optional[str] = None,
              gemini_tier: Optional[str] = None) -> dict:
    """The choice as .env-style names. {} = nothing chosen here (.env is used); "off" turns the feature off."""
    provider = (provider or "").strip().lower()
    if provider in ("", "none"):
        return {}
    if provider == "off":
        return {"AI_PROVIDER": "off"}
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider {provider!r} - use one of: {', '.join(PROVIDERS)}.")
    info = PROVIDERS[provider]
    values = {"AI_PROVIDER": provider}
    if info.key_env:
        if not (api_key or "").strip():
            raise ValueError(f"Enter the {info.display_name} API key, or choose no AI analysis for now.")
        values[info.key_env] = api_key.strip()
    if info.models_env and (models or "").strip():
        values[info.models_env] = models.strip()
    if provider == "gemini" and gemini_tier:
        values["GEMINI_TIER"] = gemini_tier.strip()
    return values


def ai_config(values: dict) -> Optional[AIConfig]:
    """The AIConfig these values give (ValueError for an invalid one, e.g. a key without a model)."""
    with stored_settings.trying(values):
        return AIConfig.from_env()


def test_connection(values: dict) -> dict:
    """A real GET of the provider's model list (nothing is saved)."""
    config = ai_config(values)
    if config is None:
        raise ValueError("Enter an API key to test.")
    name = PROVIDERS[config.provider].display_name
    try:
        models = build_provider(config).list_models()
    except ApiError as exc:
        return {"ok": False, "detail": f"{name} refused or could not be reached: {exc}"}
    return {"ok": True, "models": models[:25], "detail": f"Connected to {name}: {len(models)} model(s) available."}
