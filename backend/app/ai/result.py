"""
AnalysisFindings: the structured JSON line the model is asked to end its
answer with (app/ai/prompt.py) - a severity suggestion, the sites it thinks
are affected, and its confidence in that reading.

extract_findings() pulls that line off the end of the answer and validates
it with require() (app/api_client.py - the project's existing "valid JSON,
but is it the shape we need" check). Safety rule: a missing or invalid
block is NEVER an error - the text is returned unchanged (minus the block,
if one was found) and `findings` is None, so the worker posts the answer
exactly as it would without this feature and records outcome
success_unstructured (app/ai/worker.py, app/ai/request_history.py).
"""

import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from app.api_client import ApiInvalidData, require

ALLOWED_SEVERITIES = ("critical", "major", "minor", "cosmetic")

# The last {...} in the text, on its own paragraph, with no nested braces
# (the schema below never needs any).
_BLOCK_PATTERN = re.compile(r"\{[^{}]*\}\s*\Z")


@dataclass(frozen=True)
class AnalysisFindings:
    suggested_severity: str
    affected_site_ids: Tuple[int, ...]
    confidence: float


def extract_findings(answer: str) -> Tuple[str, Optional[AnalysisFindings]]:
    """
    (text, findings) - `text` is what split_answer() should post: `answer`
    with the trailing JSON block removed, or `answer` itself when there is
    none. `findings` is None whenever the block is missing or invalid (see
    the module docstring).
    """
    match = _BLOCK_PATTERN.search(answer)
    if match is None:
        return answer, None
    text = answer[:match.start()].rstrip()
    try:
        return text, _parse(match.group(0))
    except (ValueError, ApiInvalidData):
        return text, None


def _parse(block: str) -> AnalysisFindings:
    data = json.loads(block)
    severity = require(data, "suggested_severity", str)
    if severity not in ALLOWED_SEVERITIES:
        raise ApiInvalidData(f"'suggested_severity' is not one of {ALLOWED_SEVERITIES}")
    site_ids = require(data, "affected_site_ids", list)
    if not all(isinstance(site_id, int) and not isinstance(site_id, bool) for site_id in site_ids):
        raise ApiInvalidData("'affected_site_ids' is not a list of integers")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        raise ApiInvalidData("'confidence' is not a number between 0.0 and 1.0")
    return AnalysisFindings(severity, tuple(site_ids), float(confidence))


__all__ = ["AnalysisFindings", "ALLOWED_SEVERITIES", "extract_findings"]
