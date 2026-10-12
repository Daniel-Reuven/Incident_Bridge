"""
The provider interface every AI provider implements, including the shared
MODEL FALLBACK logic.

The rest of the AI feature (the worker in app/ai/worker.py, the prompt in
app/ai/prompt.py) only ever talks to AnalysisProvider - it never imports a
company's SDK. Each real provider lives in its own file in this folder and
is the only place its SDK is imported (see app/ai/providers/__init__.py for
the registry that picks one).

Model fallback: a provider is configured with an ORDERED list of models,
best first (for Gemini the list comes from the tier - see gemini_models.py).
analyze() tries them in order. When a model answers, that answer is used.
When a model is busy or out of quota (the provider raises ModelUnavailable),
the next model is tried. When every model was unavailable, the analysis
fails with one AnalysisError naming each model and why. Any OTHER failure
(a timeout, a rejected API key, an empty answer) stops at once: trying
another model would not help, and a timeout already used up the time.

How to add a provider:
    1. Create app/ai/providers/<name>_provider.py with a subclass of
       AnalysisProvider that implements _ask() and raises ModelUnavailable
       for "busy / rate limited / no quota" errors.
    2. Add one ProviderInfo line for it to PROVIDERS in
       app/ai/providers/__init__.py.
    3. Add its SDK as an optional dependency group in backend/pyproject.toml.
"""

from abc import ABC, abstractmethod
from typing import List, NamedTuple, Optional, Sequence


class AnalysisError(Exception):
    """
    An AI call failed in an expected way: a timeout, a rejected API key, an
    empty answer, or every model busy. The message is short and readable,
    because the worker posts it on the fault as an error comment, e.g.
    "AI analysis by Gemini (model: ...) failed: timed out after 180 seconds."
    """


class ModelUnavailable(AnalysisError):
    """
    This one model cannot take the request right now - overloaded, rate
    limited, out of (free-tier) quota, or not available to this key - so
    the next model in the list should be tried. Raised by a provider's
    _ask(); analyze() catches it and moves on.
    """


# HTTP status codes that mean "this model can't take it right now - try the
# next one", with the reason posted in the error comment if every model fails.
# Anything else (400 bad request, 401/403 bad key) fails at once: another
# model would get the same answer.
UNAVAILABLE_STATUS = {
    404: "is not available to this key (404)",
    429: "is rate limited or out of quota (429)",
    500: "had a server error (500)",
    503: "is overloaded (503)",
    504: "did not respond in time on the server (504)",
    529: "is overloaded (529)",
}


def check_unavailable(status_code: Optional[int]) -> None:
    """Raise ModelUnavailable if `status_code` means "try the next model" (see UNAVAILABLE_STATUS)."""
    if status_code in UNAVAILABLE_STATUS:
        raise ModelUnavailable(UNAVAILABLE_STATUS[status_code])


class AnalysisResult(NamedTuple):
    """What analyze() returns: the answer, and which model actually gave it (shown in the comment header)."""
    text: str
    model: str


class AnalysisProvider(ABC):
    """
    One configured AI provider with an ordered list of models.

    `display_name` is the provider's human name ("Gemini", "Claude",
    "OpenAI"); together with the model it appears in the button tooltip,
    the request/cancel comments and the header of every answer comment.
    """

    display_name = "AI"

    def __init__(self, models: Sequence[str], api_key: Optional[str], timeout_seconds: float):
        if not models:
            raise ValueError("An AI provider needs at least one model.")
        self.models: List[str] = list(models)
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        """The first (preferred) model."""
        return self.models[0]

    def analyze(self, instructions: str, incident_data: str) -> AnalysisResult:
        """
        Ask the models in order until one answers (see the module docstring).

        `instructions` tells the model what to do and how to format the answer
        (sent as the system instructions); `incident_data` is the fault and its
        comments as JSON text (sent as the user message, a separate part, so the
        model treats it as data rather than instructions). Both come from
        app/ai/prompt.py.

        Raises AnalysisError if no model answered.
        """
        failures = []
        for model in self.models:
            try:
                return AnalysisResult(self._ask(model, instructions, incident_data), model)
            except ModelUnavailable as exc:
                failures.append(f"{model} {exc}")
        if len(failures) == 1:
            raise AnalysisError(failures[0])
        raise AnalysisError(f"all {len(failures)} models were unavailable - " + "; ".join(failures))

    @abstractmethod
    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        """
        Send ONE request to ONE model and return its answer as plain text.

        Must give up after `timeout_seconds` and must not retry on its own.
        Raises ModelUnavailable when this model is busy, rate limited, out of
        quota or not available to this key (the next model is then tried),
        and AnalysisError for every other expected failure.
        """

    def refresh_models(self) -> Optional[str]:
        """
        Update self.models from the provider's live list of models, if this
        provider supports that and it is switched on (Gemini with
        GEMINI_MODEL_DISCOVERY=auto - see gemini_provider.py). Called once by
        the worker thread when it starts, before its first job. Returns a
        one-line summary for the server log, or None when there is nothing to
        do. May raise any exception; the worker then keeps the list as it is.
        """
        return None

    def label(self, model: Optional[str] = None) -> str:
        """
        For comment headers: 'Gemini (model: gemini-x)' for the model that
        answered; without a model, the configured list, e.g.
        'Gemini (models: gemini-a > gemini-b)'.
        """
        if model is not None or len(self.models) == 1:
            return f"{self.display_name} (model: {model or self.model})"
        return f"{self.display_name} (models: {' > '.join(self.models)})"

    def timeout_message(self) -> str:
        """The AnalysisError text for a timeout, shared by every provider."""
        return f"timed out after {self.timeout_seconds:g} seconds"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(models={self.models!r}, timeout_seconds={self.timeout_seconds:g})"
