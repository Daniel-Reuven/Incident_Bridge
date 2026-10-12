"""
ClaudeProvider: Anthropic's Claude models, through Anthropic's official
Python SDK (`anthropic`, installed with: pip install -e ".[claude]").

This is the only file that imports anthropic. It is loaded only when
AI_PROVIDER=claude (see app/ai/providers/__init__.py).

Models: ANTHROPIC_MODEL holds one model or a comma-separated list, best
first; when one is overloaded (529/503), rate limited (429), not found (404)
or has a server error (500), the next is tried (base.py).

Request shape: the instructions go in `system`, the incident JSON is the
user message. Timeout: the client's `timeout` (seconds), per call. Retries:
the SDK retries twice by default, so max_retries=0 keeps one call within the
timeout. The Messages API requires max_tokens; MAX_OUTPUT_TOKENS leaves
plenty of room for an answer of up to three comments.
"""

import anthropic

from app.ai.providers.base import AnalysisError, AnalysisProvider, check_unavailable

MAX_OUTPUT_TOKENS = 8000


class ClaudeProvider(AnalysisProvider):
    display_name = "Claude"

    def __init__(self, models, api_key: str, timeout_seconds: float):
        super().__init__(models, api_key, timeout_seconds)
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=instructions,
                messages=[{"role": "user", "content": incident_data}],
            )
        except anthropic.APITimeoutError:
            raise AnalysisError(self.timeout_message()) from None
        except anthropic.APIStatusError as exc:
            check_unavailable(exc.status_code)       # busy / no quota -> next model
            raise AnalysisError(f"the Claude API returned an error ({exc.status_code}): {exc.message}") from None
        except anthropic.APIConnectionError as exc:
            raise AnalysisError(f"could not reach the Claude API: {exc}") from None

        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise AnalysisError(f"Claude ({model}) returned an empty answer.")
        return text
