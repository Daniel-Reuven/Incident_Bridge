"""
GeminiProvider: Google's Gemini models, through Google's official Python SDK
(`google-genai`, installed with: pip install -e ".[gemini]").

This is the only file that imports google-genai. It is loaded only when
AI_PROVIDER=gemini (see app/ai/providers/__init__.py), so the app runs fine
without the package when another provider is used.

Which models: an ordered list, best first - by default the list for the
key's tier (GEMINI_TIER=free or paid, see gemini_models.py), or your own
GEMINI_MODELS. With GEMINI_MODEL_DISCOVERY=auto, refresh_models() asks Google
at startup which models the key can use, removes retired ones and adds newer
ones to the front (the rules are in gemini_models.refresh_model_list). When a model is overloaded (503), rate limited or out of
quota (429), not available to the key (404) or has a server error (500/504),
the next model is tried (the fallback loop is in base.py).

Request shape: the instructions go in `system_instruction`, the incident JSON
is the user content. Timeout: HttpOptions(timeout=...) in MILLISECONDS, per
call. Retries: none - the SDK only retries when retry_options are given, and
we give none, so one call never takes longer than the timeout.
"""

from typing import List, Optional

import httpx
from google import genai
from google.genai import errors, types

from app.ai.providers.base import AnalysisError, AnalysisProvider, check_unavailable
from app.ai.providers.gemini_models import refresh_model_list

# The model listing is a quick call: it gets its own short timeout, not the analysis timeout.
LIST_TIMEOUT_SECONDS = 15


class GeminiProvider(AnalysisProvider):
    display_name = "Gemini"

    def __init__(self, models, api_key: str, timeout_seconds: float,
                 tier: str = "free", discovery: bool = False, extend: bool = True):
        """`tier`, `discovery`, `extend`: see gemini_models.gemini_settings()."""
        super().__init__(models, api_key, timeout_seconds)
        self.tier = tier
        self.discovery = discovery
        self.extend = extend
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )

    def refresh_models(self) -> Optional[str]:
        """With discovery on: update self.models from Google's list (see the module docstring)."""
        if not self.discovery:
            return None
        result = refresh_model_list(self.available_models(), self.models, self.tier, self.extend)
        self.models = result.models
        return result.summary()

    def available_models(self) -> List[str]:
        """The models this key can call for text generation, e.g. ["gemini-3.8-flash", ...]."""
        listing = self._client.models.list(config=types.ListModelsConfig(
            page_size=100, http_options=types.HttpOptions(timeout=LIST_TIMEOUT_SECONDS * 1000)))
        return [model.name.split("/")[-1] for model in listing
                if "generateContent" in (model.supported_actions or [])]

    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=incident_data,
                config=types.GenerateContentConfig(system_instruction=instructions),
            )
        except httpx.TimeoutException:
            raise AnalysisError(self.timeout_message()) from None
        except errors.APIError as exc:
            check_unavailable(exc.code)              # busy / no quota -> next model
            raise AnalysisError(f"the Gemini API returned an error ({exc.code}): {exc.message}") from None
        except httpx.HTTPError as exc:
            raise AnalysisError(f"could not reach the Gemini API: {exc}") from None

        text = response.text
        if not text or not text.strip():
            raise AnalysisError(f"Gemini ({model}) returned an empty answer.")
        return text
