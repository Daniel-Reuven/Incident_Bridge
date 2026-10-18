"""
GeminiProvider: Google's Gemini models, through the Gemini REST API
(https://ai.google.dev/api) called with the project's own ApiClient
(app/api_client.py, `requests`) - no Google SDK (v0.5.0).

Which models: an ordered list, best first - by default the list for the
key's tier (GEMINI_TIER=free or paid, see gemini_models.py), or your own
GEMINI_MODELS. With GEMINI_MODEL_DISCOVERY=auto, refresh_models() asks Google
(GET /models) when the worker thread starts (first analysis request) which
models the key can use, removes retired ones and adds newer ones to the
front (the rules are in gemini_models.refresh_model_list). When a model is
overloaded (503), rate limited or out of quota (429), not available to the
key (404) or has a server error (500/504), the next model is tried (the
fallback loop is in base.py; the error mapping in http_provider.py).

Requests:
    POST {BASE_URL}/models/{model}:generateContent
         {"systemInstruction": {"parts": [{"text": <instructions>}]},
          "contents": [{"role": "user", "parts": [{"text": <incident JSON>}]}]}
         reply: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    GET  {BASE_URL}/models?pageSize=100[&pageToken=...]   (model discovery)
         reply: {"models": [{"name": "models/gemini-...",
                             "supportedGenerationMethods": ["generateContent", ...]}],
                 "nextPageToken": "..."}
The key goes in the x-goog-api-key header (never in the URL, so it can't
end up in a log line). The listing GET is retried on a timeout or
connection error (ApiClient.get_json: 3 attempts); the analysis POST is not.
"""

from typing import Dict, List, Optional

from app.ai.providers.gemini_models import refresh_model_list
from app.ai.providers.http_provider import HttpProvider
from app.api_client import require

# The model listing is a quick call: it gets its own short timeout, not the analysis timeout.
LIST_TIMEOUT_SECONDS = 15
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 10                 # a safety stop; Google lists a few dozen models today


class GeminiProvider(HttpProvider):
    display_name = "Gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, models, api_key: str, timeout_seconds: float,
                 tier: str = "free", discovery: bool = False, extend: bool = True, **http_options):
        """`tier`, `discovery`, `extend`: see gemini_models.gemini_settings(). `http_options`: see HttpProvider."""
        super().__init__(models, api_key, timeout_seconds, **http_options)
        self.tier = tier
        self.discovery = discovery
        self.extend = extend

    def _headers(self, api_key: str) -> Dict[str, str]:
        return {"x-goog-api-key": api_key}

    def _path(self, model: str) -> str:
        return f"/models/{model}:generateContent"

    def _body(self, model: str, instructions: str, incident_data: str) -> dict:
        return {"systemInstruction": {"parts": [{"text": instructions}]},
                "contents": [{"role": "user", "parts": [{"text": incident_data}]}]}

    def _answer_text(self, model: str, reply: dict) -> str:
        """The text parts of the first candidate (thought summaries skipped); "" if it has none (e.g. blocked)."""
        candidates = require(reply, "candidates", list)
        if not candidates:
            return ""
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        if content is None:                      # stopped before writing anything (e.g. SAFETY)
            return ""
        parts = require(content, "parts", list, where="the candidate's content")
        return "".join(part["text"] for part in parts
                       if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought"))

    def refresh_models(self) -> Optional[str]:
        """With discovery on: update self.models from Google's list (see the module docstring)."""
        if not self.discovery:
            return None
        result = refresh_model_list(self.available_models(), self.models, self.tier, self.extend)
        self.models = result.models
        return result.summary()

    def available_models(self) -> List[str]:
        """The models this key can call for text generation, e.g. ["gemini-3.8-flash", ...], in Google's order."""
        names: List[str] = []
        token = None
        for _ in range(MAX_LIST_PAGES):
            params = {"pageSize": LIST_PAGE_SIZE, **({"pageToken": token} if token else {})}
            page = self._client.get_json("/models", params=params, timeout_seconds=LIST_TIMEOUT_SECONDS,
                                         log_fields={"provider": self.display_name, "purpose": "model listing"})
            for entry in page.get("models") or []:
                name = require(entry, "name", str, where="a listed model")
                if "generateContent" in (entry.get("supportedGenerationMethods") or []):
                    names.append(name.split("/")[-1])
            token = page.get("nextPageToken")
            if not token:
                break
        return names
