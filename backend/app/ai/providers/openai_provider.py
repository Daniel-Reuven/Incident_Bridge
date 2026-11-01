"""
OpenAIProvider: OpenAI's models (the ones behind ChatGPT), through the
OpenAI Responses REST API (https://platform.openai.com/docs/api-reference/responses)
called with the project's own ApiClient (app/api_client.py, `requests`) -
no OpenAI SDK (v0.5.0).

Models: OPENAI_MODEL holds one model or a comma-separated list, best first;
when one is rate limited or out of quota (429), unavailable (503), not found
(404) or has a server error (500), the next is tried (base.py).

Request:
    POST {BASE_URL}/responses   header Authorization: Bearer <key>
         {"model": ..., "instructions": <instructions>, "input": <incident JSON>}
    reply: {"output": [{"type": "message",
                        "content": [{"type": "output_text", "text": "..."}]}, ...]}
(The SDK's `output_text` is a convenience the SDK computes; the REST reply
has only `output`, so _answer_text() joins the output_text parts itself.)
"""

from typing import Dict

from app.ai.providers.http_provider import HttpProvider
from app.api_client import require


class OpenAIProvider(HttpProvider):
    display_name = "OpenAI"
    BASE_URL = "https://api.openai.com/v1"

    def _headers(self, api_key: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def _path(self, model: str) -> str:
        return "/responses"

    def _body(self, model: str, instructions: str, incident_data: str) -> dict:
        return {"model": model, "instructions": instructions, "input": incident_data}

    def _answer_text(self, model: str, reply: dict) -> str:
        """The text of every output_text part of every "message" item, in order."""
        texts = []
        for item in require(reply, "output", list):
            if isinstance(item, dict) and item.get("type") == "message":
                for part in require(item, "content", list, where="a message item"):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(require(part, "text", str, where="an output_text part"))
        return "".join(texts)
