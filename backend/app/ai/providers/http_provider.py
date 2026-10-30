"""
HttpProvider: the shared base of the three real AI providers (Gemini,
Claude, OpenAI), which call their company's REST API directly through the
project's own ApiClient (app/api_client.py, built on `requests`) - no
company SDK is used (v0.5.0).

Every provider sends ONE POST per model attempt and turns the JSON reply
into text. What differs between the companies is only the "shape" of the
call, so each subclass fills in four small methods (a Template Method -
_ask() below is the fixed algorithm):

    _headers(api_key)                      how the key is sent
    _path(model)                           where the request goes
    _body(model, instructions, data)       the request JSON
    _answer_text(model, reply)             the text in the reply JSON
                                           (checked with require(); raises
                                           ApiInvalidData if the shape is wrong)

_ask() maps every ApiClient failure to the provider contract in base.py:

    ApiTimeout           -> AnalysisError "timed out after N seconds"
    ApiHTTPError 404/429/500/503/504/529 -> ModelUnavailable (try the next model)
    ApiHTTPError other (400, 401, 403)   -> AnalysisError (stop - another model would get the same answer)
    ApiConnectionError   -> AnalysisError "could not reach the X API"
    ApiInvalidJSON / ApiInvalidData      -> AnalysisError "unexpected reply"
    an empty answer      -> AnalysisError "returned an empty answer"

The analysis POST is never retried (ApiClient.post_json): model fallback is
the only "retry", and a timeout already used up the time budget.

The ApiClient is created in the constructor from BASE_URL, or passed in
(`client=`) - tests pass one pointing at a local stub server.
"""

from abc import abstractmethod
from typing import Dict, List, Optional

from app.ai.log import log
from app.ai.providers.base import AnalysisError, AnalysisProvider, check_unavailable
from app.api_client import (ApiClient, ApiConnectionError, ApiHTTPError, ApiInvalidData, ApiInvalidJSON,
                            ApiTimeout)


class HttpProvider(AnalysisProvider):
    BASE_URL = ""      # the company's API root - set by every subclass
    MODELS_PATH = "/models"   # GET: the models this key may use ({"data": [{"id": ...}]}, Anthropic and OpenAI)

    def __init__(self, models, api_key: Optional[str], timeout_seconds: float,
                 client: Optional[ApiClient] = None, base_url: Optional[str] = None):
        """`client`: a ready ApiClient (tests); otherwise one is built for `base_url` (default BASE_URL)."""
        super().__init__(models, api_key, timeout_seconds)
        self._client = client or ApiClient(base_url or self.BASE_URL, headers=self._headers(api_key or ""),
                                           timeout_seconds=timeout_seconds, name=f"the {self.display_name} API",
                                           logger=log.getChild("http"))

    @abstractmethod
    def _headers(self, api_key: str) -> Dict[str, str]:
        """The headers that carry the API key (and API version, if any)."""

    @abstractmethod
    def _path(self, model: str) -> str:
        """The request path below BASE_URL."""

    @abstractmethod
    def _body(self, model: str, instructions: str, incident_data: str) -> dict:
        """The request JSON: `instructions` as the system part, `incident_data` as the user message."""

    @abstractmethod
    def _answer_text(self, model: str, reply: dict) -> str:
        """The answer text in the reply JSON ("" if there is none). Raises ApiInvalidData for a wrong shape."""

    def list_models(self) -> List[str]:
        """GET MODELS_PATH - a real GET with the client's retry; ApiError when the key or connection fails."""
        reply = self._client.get_json(self.MODELS_PATH, timeout_seconds=15,
                                      log_fields={"provider": self.display_name, "purpose": "connection test"})
        return [entry["id"] for entry in reply.get("data") or [] if isinstance(entry, dict) and "id" in entry]

    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        api = f"the {self.display_name} API"
        try:
            reply = self._client.post_json(self._path(model), self._body(model, instructions, incident_data),
                                           log_fields={"provider": self.display_name, "model": model})
            text = self._answer_text(model, reply)
        except ApiTimeout:
            raise AnalysisError(self.timeout_message()) from None
        except ApiHTTPError as exc:
            check_unavailable(exc.status_code)           # busy / no quota -> next model
            raise AnalysisError(f"{api} returned an error ({exc.status_code}): {exc.message}") from None
        except ApiConnectionError as exc:
            raise AnalysisError(f"could not reach {api}: {exc}") from None
        except (ApiInvalidJSON, ApiInvalidData) as exc:
            raise AnalysisError(f"{api} returned an unexpected reply: {exc}") from None

        if not text.strip():
            raise AnalysisError(f"{self.display_name} ({model}) returned an empty answer.")
        return text
