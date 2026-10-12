"""
The AI provider registry: which providers exist, which environment
variables configure each one, and the functions that check and create the
one selected by AI_PROVIDER (see app/ai/config.py):

- check_sdk_installed(config): at STARTUP - checks that the selected
  provider's SDK package is installed WITHOUT importing it
  (importlib.util.find_spec), so a missing package still stops the app with
  the exact pip command, but no SDK memory is used yet.
- build_provider(config): on the FIRST ANALYSIS (via ProviderLoader in
  loader.py, called by the worker thread) - imports the provider's module,
  and so its SDK, and creates the provider and its client.
- ProviderLoader (loader.py): holds the provider's name and models from the
  settings and calls build_provider() the first time it is needed.

Each provider's module (and so its SDK) is imported ONLY when that provider
is selected AND first used, using importlib. That keeps every SDK optional:
someone using Claude never needs google-genai or openai installed.

Models: each provider's model setting holds ONE model or a comma-separated
list, best first - the provider falls back along the list when a model is
busy or out of quota (see base.py). A provider may also have a
`default_models` function, used when its model setting is empty; Gemini's
picks the list for the key's tier (see gemini_models.py). And it may have
`extra_settings`: provider-specific settings read (and checked) at startup
and passed to its constructor - Gemini's tier and model discovery.

`display_name` is also in the registry (not only on the provider class), so
the page can show "Analyze using Gemini" before the provider is created.
tests/test_ai/test_config.py checks that it matches the class.

Only ONE provider is active at a time today. The registry already lists
them all, so supporting several later only needs one ProviderLoader per
provider.

To add a provider: write its module (see base.py), then add one
ProviderInfo line to PROVIDERS below.
"""

import importlib
import importlib.util
from typing import Callable, NamedTuple, Optional, Tuple

from app.ai.providers.base import AnalysisError, AnalysisProvider, AnalysisResult, ModelUnavailable
from app.ai.providers.gemini_models import default_gemini_models, gemini_settings


class ProviderInfo(NamedTuple):
    module: str                 # module name inside app/ai/providers/
    class_name: str             # the AnalysisProvider subclass in that module
    display_name: str           # its human name, the same as the class's display_name
    sdk_module: Optional[str]   # the SDK package it imports, checked at startup (None = no SDK)
    key_env: Optional[str]      # environment variable holding the API key (None = no key needed)
    models_env: Optional[str]   # environment variable holding the model(s) (None = fixed model)
    pip_extra: Optional[str]    # the pyproject.toml extra that installs its SDK
    default_models: Optional[Callable[[], Tuple[str, ...]]] = None   # used when models_env is empty
    extra_settings: Optional[Callable[[], dict]] = None   # provider-specific settings, passed to its constructor


# AI_PROVIDER value -> how to build it. The first entry is the default
# provider (used when AI_PROVIDER is not set).
PROVIDERS = {
    "gemini": ProviderInfo("gemini_provider", "GeminiProvider", "Gemini", "google.genai",
                           "GEMINI_API_KEY", "GEMINI_MODELS", "gemini", default_gemini_models, gemini_settings),
    "claude": ProviderInfo("claude_provider", "ClaudeProvider", "Claude", "anthropic",
                           "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude"),
    "openai": ProviderInfo("openai_provider", "OpenAIProvider", "OpenAI", "openai",
                           "OPENAI_API_KEY", "OPENAI_MODEL", "openai"),
    # tests / trying it without a key - no SDK, one fixed model
    "fake": ProviderInfo("fake_provider", "FakeProvider", "Fake AI", None, None, None, None,
                         lambda: ("fake-model",)),
}
DEFAULT_PROVIDER = next(iter(PROVIDERS))


def _missing_sdk(provider: str, info: ProviderInfo, detail: str) -> ValueError:
    return ValueError(
        f"AI_PROVIDER={provider} needs its SDK package, which is not installed ({detail}). "
        f'Install it from backend/ with: pip install -e ".[{info.pip_extra}]"'
    )


def check_sdk_installed(config) -> None:
    """
    Raise ValueError (with the pip command) if the SDK of the provider in
    `config` is not installed. Does NOT import the SDK - find_spec only
    looks for the package on disk - so startup stays light. Called by
    app/ai/setup.py at startup.
    """
    info = PROVIDERS[config.provider]
    if info.sdk_module is None:
        return
    try:
        found = importlib.util.find_spec(info.sdk_module) is not None
    except ImportError:                     # the parent package ("google") is missing
        found = False
    if not found:
        raise _missing_sdk(config.provider, info, f"No module named {info.sdk_module!r}")


def build_provider(config) -> AnalysisProvider:
    """
    Create the provider described by `config` (an AIConfig - see
    app/ai/config.py). Raises ValueError with an installation hint if its
    SDK package is not installed.

    This IMPORTS the SDK (tens of MB that stay for the life of the process),
    so the app calls it only through ProviderLoader, on the first analysis.
    """
    info = PROVIDERS[config.provider]
    try:
        module = importlib.import_module(f"app.ai.providers.{info.module}")
    except ImportError as exc:
        raise _missing_sdk(config.provider, info, str(exc)) from None
    provider_class = getattr(module, info.class_name)
    if info.models_env is None:   # the fake provider has a fixed model name (its default)
        return provider_class(api_key=config.api_key, timeout_seconds=config.timeout_seconds)
    return provider_class(models=config.models, api_key=config.api_key, timeout_seconds=config.timeout_seconds,
                          **config.extra)


from app.ai.providers.loader import ProviderLoader  # noqa: E402  (loader imports base only)


def provider_loader(config) -> ProviderLoader:
    """A ProviderLoader for `config`: name and models from the settings, the provider built on first use."""
    return ProviderLoader(PROVIDERS[config.provider].display_name, config.models, lambda: build_provider(config))


__all__ = ["AnalysisError", "AnalysisProvider", "AnalysisResult", "ModelUnavailable",
           "PROVIDERS", "DEFAULT_PROVIDER", "ProviderInfo", "ProviderLoader",
           "build_provider", "check_sdk_installed", "provider_loader"]
