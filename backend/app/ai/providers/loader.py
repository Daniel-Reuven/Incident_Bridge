"""
ProviderLoader: the configured AI provider, created only when it is first
needed (v0.4.5).

Creating a provider imports its company's SDK and builds its client, which
costs memory for as long as the server runs (measured: google-genai about
+21 MB, anthropic about +32 MB, openai about +12 MB) - and a Python process
never gives that memory back, even if the SDK is "unloaded". So the server
does NOT create the provider at startup. It only remembers what the
settings say (the provider's name and model list) and creates the provider
the first time an analysis actually runs. A server where nobody ever presses
"Analyze" never loads an SDK at all.

Until then, everything the page needs - "Analyze using Gemini", the model
list in the tooltip - comes from the settings (`display_name`, `models`).
Once the provider exists, `models` is ITS list, which may have been updated
by Gemini model discovery.

Thread-safe: get() may be called from several threads; the provider is
built exactly once. If building fails (should not happen - startup already
checked that the SDK is installed), get() raises and the next call tries
again.

Only ONE provider is configured at a time (AI_PROVIDER), so the worker holds
one loader. Supporting several providers later means one loader per
provider (e.g. a dict keyed by AI_PROVIDER value), each loading its SDK on
first use - nothing here assumes there is only one.

Usage:
    loader = ProviderLoader("Gemini", ("gemini-a", "gemini-b"), lambda: build_provider(config))
    loader.label()      # 'Gemini (models: gemini-a > gemini-b)' - no SDK import
    provider = loader.get()   # imports the SDK and builds the client, once
    # Tests can pass a ready provider:
    loader = ProviderLoader.ready(FakeProvider())
"""

import threading
from typing import Callable, List, Optional, Sequence

from app.ai.providers.base import AnalysisProvider, provider_label


class ProviderLoader:
    def __init__(self, display_name: str, models: Sequence[str], build: Callable[[], AnalysisProvider]):
        """
        `display_name`: the provider's human name ("Gemini"); `models`: the
        configured model list, best first; `build`: creates the provider
        (normally `lambda: build_provider(config)`).
        """
        self.display_name = display_name
        self._configured_models: List[str] = list(models)
        self._build = build
        self._provider: Optional[AnalysisProvider] = None
        self._lock = threading.Lock()

    @classmethod
    def ready(cls, provider: AnalysisProvider) -> "ProviderLoader":
        """A loader around a provider that already exists (tests, or a provider built elsewhere)."""
        loader = cls(provider.display_name, provider.models, lambda: provider)
        loader._provider = provider
        return loader

    @property
    def loaded(self) -> bool:
        """True once the provider (and its SDK) has been created."""
        return self._provider is not None

    @property
    def provider(self) -> Optional[AnalysisProvider]:
        """The provider if it has been created, else None (never creates it)."""
        return self._provider

    def get(self) -> AnalysisProvider:
        """The provider, creating it (importing its SDK) the first time. Raises whatever building raises."""
        with self._lock:
            if self._provider is None:
                self._provider = self._build()
            return self._provider

    def set(self, provider: AnalysisProvider) -> None:
        """Replace the provider (tests use this to swap in a slower fake one)."""
        with self._lock:
            self._provider = provider
            self.display_name = provider.display_name

    @property
    def models(self) -> List[str]:
        """The live model list once created (it may have been refreshed), else the configured one."""
        provider = self._provider
        return list(provider.models) if provider is not None else list(self._configured_models)

    @property
    def model(self) -> str:
        """The preferred (first) model."""
        return self.models[0]

    def label(self, model: Optional[str] = None) -> str:
        """Same as AnalysisProvider.label(), without creating the provider."""
        return provider_label(self.display_name, self.models, model)

    def __repr__(self) -> str:
        return f"ProviderLoader({self.display_name!r}, models={self.models!r}, loaded={self.loaded})"
