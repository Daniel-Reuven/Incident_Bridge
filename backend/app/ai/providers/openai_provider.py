"""
OpenAIProvider: OpenAI's models (the ones behind ChatGPT), through OpenAI's
official Python SDK (`openai`, installed with: pip install -e ".[openai]").

This is the only file that imports openai. It is loaded only when
AI_PROVIDER=openai (see app/ai/providers/__init__.py).

Models: OPENAI_MODEL holds one model or a comma-separated list, best first;
when one is rate limited or out of quota (429), unavailable (503), not found
(404) or has a server error (500), the next is tried (base.py).

Request shape: the Responses API - the instructions go in `instructions`,
the incident JSON is the `input`. Timeout: the client's `timeout` (seconds),
per call. Retries: the SDK retries twice by default, so max_retries=0 keeps
one call within the timeout.
"""

import openai

from app.ai.providers.base import AnalysisError, AnalysisProvider, check_unavailable


class OpenAIProvider(AnalysisProvider):
    display_name = "OpenAI"

    def __init__(self, models, api_key: str, timeout_seconds: float):
        super().__init__(models, api_key, timeout_seconds)
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        try:
            response = self._client.responses.create(
                model=model,
                instructions=instructions,
                input=incident_data,
            )
        except openai.APITimeoutError:
            raise AnalysisError(self.timeout_message()) from None
        except openai.APIStatusError as exc:
            check_unavailable(exc.status_code)       # busy / no quota -> next model
            raise AnalysisError(f"the OpenAI API returned an error ({exc.status_code}): {exc.message}") from None
        except openai.APIConnectionError as exc:
            raise AnalysisError(f"could not reach the OpenAI API: {exc}") from None

        text = response.output_text
        if not text or not text.strip():
            raise AnalysisError(f"OpenAI ({model}) returned an empty answer.")
        return text
