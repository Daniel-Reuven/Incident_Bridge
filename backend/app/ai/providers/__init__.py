"""
The AI provider registry: which providers exist, which environment
variables configure each one, and the functions that create the one
selected by AI_PROVIDER (see app/ai/config.py):

- build_provider(config): on the FIRST ANALYSIS (via ProviderLoader in
  loader.py, called by the worker thread) - imports the provider's module
  and creates the provider and its HTTP client.
- ProviderLoader (loader.py): holds the provider's name and models from the
  settings and calls build_provider() the first time it is needed.

This is a FACTORY: build_provider() picks the class to create from the
AI_PROVIDER value (data), and the rest of the app only ever uses the
AnalysisProvider interface it returns (the providers are interchangeable
STRATEGIES for "ask a model").

Since v0.5.0 no company SDK is used: every real provider calls its REST API
through the project's own ApiClient (app/api_client.py, built on
`requests`, a normal dependency) - so there is nothing extra to install per
provider.

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

To add a provider: write its module (see http_provider.py), then add one
ProviderInfo line to PROVIDERS below.
"""

import importlib
from typing import Callable, NamedTuple, Optional, Tuple

from app.ai.providers.base import AnalysisError, AnalysisProvider, AnalysisResult, ModelUnavailable
from app.ai.providers.gemini_models import default_gemini_models, gemini_settings


class ProviderInfo(NamedTuple):
    module: str                 # module name inside app/ai/providers/
    class_name: str             # the AnalysisProvider subclass in that module
    display_name: str           # its human name, the same as the class's display_name
    key_env: Optional[str]      # environment variable holding the API key (None = no key needed)
    models_env: Optional[str]   # environment variable holding the model(s) (None = fixed model)
    default_models: Optional[Callable[[], Tuple[str, ...]]] = None   # used when models_env is empty
    extra_settings: Optional[Callable[[], dict]] = None   # provider-specific settings, passed to its constructor


# AI_PROVIDER value -> how to build it. The first entry is the default
# provider (used when AI_PROVIDER is not set).
PROVIDERS = {
    "gemini": ProviderInfo("gemini_provider", "GeminiProvider", "Gemini",
                           "GEMINI_API_KEY", "GEMINI_MODELS", default_gemini_models, gemini_settings),
    "claude": ProviderInfo("claude_provider", "ClaudeProvider", "Claude", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    "openai": ProviderInfo("openai_provider", "OpenAIProvider", "OpenAI", "OPENAI_API_KEY", "OPENAI_MODEL"),
    # tests / trying it without a key - no network, one fixed model
    "fake": ProviderInfo("fake_provider", "FakeProvider", "Fake AI", None, None, lambda: ("fake-model",)),
}
DEFAULT_PROVIDER = next(iter(PROVIDERS))


def build_provider(config) -> AnalysisProvider:
    """
    Create the provider described by `config` (an AIConfig - see
    app/ai/config.py). The app calls it only through ProviderLoader, on the
    first analysis.
    """
    info = PROVIDERS[config.provider]
    module = importlib.import_module(f"app.ai.providers.{info.module}")
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
           "build_provider", "provider_loader"]
