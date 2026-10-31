"""
FakeProvider: an AI provider that never touches the network.

Two uses:
- the automated tests (tests/test_ai/), which need predictable answers,
  errors, busy models and timing without any API key;
- trying the whole feature locally without an API key: set AI_PROVIDER=fake
  in backend/.env and every analysis returns a short canned answer.

The canned answer ends with the structured findings line a real model is
asked for (app/ai/prompt.py, app/ai/result.py): the sites the fault and its
comments mention ("Site 1007"), its current severity and a fixed confidence
- so the findings -> site check -> report flow can be tried without a key.
"""

import json
import time
from typing import Iterable, Optional, Sequence

from app.ai.providers.base import AnalysisError, AnalysisProvider, ModelUnavailable
from app.site_scan import ID_PATTERN


class FakeProvider(AnalysisProvider):
    display_name = "Fake AI"

    def __init__(self, model: str = "fake-model", api_key: Optional[str] = None, timeout_seconds: float = 180,
                 answer: Optional[str] = None, error: Optional[str] = None, delay_seconds: float = 0,
                 models: Optional[Sequence[str]] = None, busy_models: Iterable[str] = ()):
        """
        `model` / `models`: one model name, or a list (best first) to test fallback.
        `answer`: the text to return (default: a canned answer built from the incident).
        `error`: if set, every call raises AnalysisError(error) instead of answering.
        `delay_seconds`: how long each call takes, to simulate a slow model.
        `busy_models`: models that answer "overloaded", so the next one is tried.
        """
        super().__init__(models or [model], api_key, timeout_seconds)
        self.answer = answer
        self.error = error
        self.delay_seconds = delay_seconds
        self.busy_models = set(busy_models)
        self.calls = []   # (instructions, incident_data) of every call, for tests
        self.asked = []   # the model of every call, in order, for tests

    def _ask(self, model: str, instructions: str, incident_data: str) -> str:
        self.calls.append((instructions, incident_data))
        self.asked.append(model)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if model in self.busy_models:
            raise ModelUnavailable("is overloaded (503)")
        if self.error:
            raise AnalysisError(self.error)
        if self.answer is not None:
            return self.answer
        data = json.loads(incident_data)
        return (
            "Summary:\n"
            f"This is a canned answer from the fake provider for \"{data['incident']['title']}\".\n\n"
            "Likely cause:\n- Not analyzed: the fake provider does not call a real model.\n\n"
            f"Evidence:\n- The fault has {len(data['comments'])} comment(s).\n\n"
            "Suggested next steps:\n1. Set AI_PROVIDER to gemini, claude or openai in backend/.env for a real analysis.\n\n"
            "Missing information:\n- Everything a real model would ask for.\n\n"
            "Severity assessment:\n- Not assessed.\n\n"
            + json.dumps(canned_findings(data))
        )


def canned_findings(data: dict) -> dict:
    """The findings line of the canned answer: the sites the fault mentions (by id), its own severity."""
    texts = [data["incident"].get("title", ""), data["incident"].get("description", "")]
    texts += [comment.get("text", "") for comment in data.get("comments", [])]
    site_ids = sorted({int(match) for text in texts for match in ID_PATTERN.findall(text)})
    severity = str(data["incident"].get("severity", "")).lower()
    return {"suggested_severity": severity if severity in ("critical", "major", "minor") else "major",
            "affected_site_ids": site_ids, "confidence": 0.5}
