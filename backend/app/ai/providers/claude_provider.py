"""
ClaudeProvider: Anthropic's Claude models, through the Claude Messages REST
API (https://docs.claude.com/en/api/messages) called with the project's own
ApiClient (app/api_client.py, `requests`) - no Anthropic SDK (v0.5.0).

Models: ANTHROPIC_MODEL holds one model or a comma-separated list, best
first; when one is overloaded (529/503), rate limited (429), not found (404)
or has a server error (500), the next is tried (base.py).

Request:
    POST {BASE_URL}/messages   headers x-api-key, anthropic-version
         {"model": ..., "max_tokens": MAX_OUTPUT_TOKENS, "system": <instructions>,
          "messages": [{"role": "user", "content": <incident JSON>}]}
    reply: {"content": [{"type": "text", "text": "..."}, ...]}
The Messages API requires max_tokens; MAX_OUTPUT_TOKENS leaves plenty of
room for an answer of up to three comments.
"""

from typing import Dict

from app.ai.providers.http_provider import HttpProvider
from app.api_client import require

MAX_OUTPUT_TOKENS = 8000
API_VERSION = "2023-06-01"


class ClaudeProvider(HttpProvider):
    display_name = "Claude"
    BASE_URL = "https://api.anthropic.com/v1"

    def _headers(self, api_key: str) -> Dict[str, str]:
        return {"x-api-key": api_key, "anthropic-version": API_VERSION}

    def _path(self, model: str) -> str:
        return "/messages"

    def _body(self, model: str, instructions: str, incident_data: str) -> dict:
        return {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": instructions,
                "messages": [{"role": "user", "content": incident_data}]}

    def _answer_text(self, model: str, reply: dict) -> str:
        """The text of every "text" block, in order (other block types are skipped)."""
        blocks = require(reply, "content", list)
        return "".join(require(block, "text", str, where="a text block")
                       for block in blocks if isinstance(block, dict) and block.get("type") == "text")
