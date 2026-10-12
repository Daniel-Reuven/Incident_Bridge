"""
The AI provider registry: which providers exist, which environment
variables configure each one, and build_provider(), which creates the one
selected by AI_PROVIDER (see app/ai/config.py).

Each provider's module (and so its SDK) is imported ONLY when that provider
is selected, using importlib. That keeps every SDK optional: someone using
Claude never needs google-genai or openai installed. If the selected
provider's package is missing, build_provider() raises ValueError with the
exact pip command to run.

Models: each provider's model setting holds ONE model or a comma-separated
list, best first - the provider falls back along the list when a model is
busy or out of quota (see base.py). A provider may also have a
`default_models` function, used when its model setting is empty; Gemini's
picks the list for the key's tier (see gemini_models.py). And it may have
`extra_settings`: provider-specific settings read (and checked) at startup
and passed to its constructor - Gemini's tier and model discovery.

To add a provider: write its module (see base.py), then add one
ProviderInfo line to PROVIDERS below.
"""

import importlib
from typing import Callable, NamedTuple, Optional, Tuple

from app.ai.providers.base import AnalysisError, AnalysisProvider, AnalysisResult, ModelUnavailable
from app.ai.providers.gemini_models import default_gemini_models, gemini_settings


class ProviderInfo(NamedTuple):
    module: str                 # module name inside app/ai/providers/
    class_name: str             # the AnalysisProvider subclass in that module
    key_env: Optional[str]      # environment variable holding the API key (None = no key needed)
    models_env: Optional[str]   # environment variable holding the model(s) (None = fixed model)
    pip_extra: Optional[str]    # the pyproject.toml extra that installs its SDK
    default_models: Optional[Callable[[], Tuple[str, ...]]] = None   # used when models_env is empty
    extra_settings: Optional[Callable[[], dict]] = None   # provider-specific settings, passed to its constructor


# AI_PROVIDER value -> how to build it. The first entry is the default
# provider (used when AI_PROVIDER is not set).
PROVIDERS = {
    "gemini": ProviderInfo("gemini_provider", "GeminiProvider", "GEMINI_API_KEY", "GEMINI_MODELS", "gemini",
                           default_gemini_models, gemini_settings),
    "claude": ProviderInfo("claude_provider", "ClaudeProvider", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude"),
    "openai": ProviderInfo("openai_provider", "OpenAIProvider", "OPENAI_API_KEY", "OPENAI_MODEL", "openai"),
    "fake": ProviderInfo("fake_provider", "FakeProvider", None, None, None),   # tests / trying it without a key
}
DEFAULT_PROVIDER = next(iter(PROVIDERS))


def build_provider(config) -> AnalysisProvider:
    """
    Create the provider described by `config` (an AIConfig - see
    app/ai/config.py). Raises ValueError with an installation hint if its
    SDK package is not installed.
    """
    info = PROVIDERS[config.provider]
    try:
        module = importlib.import_module(f"app.ai.providers.{info.module}")
    except ImportError as exc:
        raise ValueError(
            f"AI_PROVIDER={config.provider} needs its SDK package, which is not installed ({exc}). "
            f'Install it from backend/ with: pip install -e ".[{info.pip_extra}]"'
        ) from None
    provider_class = getattr(module, info.class_name)
    if info.models_env is None:   # the fake provider has a fixed model name
        return provider_class(api_key=config.api_key, timeout_seconds=config.timeout_seconds)
    return provider_class(models=config.models, api_key=config.api_key, timeout_seconds=config.timeout_seconds,
                          **config.extra)


__all__ = ["AnalysisError", "AnalysisProvider", "AnalysisResult", "ModelUnavailable",
           "PROVIDERS", "DEFAULT_PROVIDER", "ProviderInfo", "build_provider"]
