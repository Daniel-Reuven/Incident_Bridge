"""
Unit tests for app/ai/result.py: extracting and validating the trailing
JSON findings block the model is asked to add (app/ai/prompt.py). The
safety rule under test throughout: a missing or invalid block is NEVER an
error - extract_findings() just returns None for `findings`.

Run from backend/:  python -m pytest tests/test_ai/test_result.py -v
"""

import pytest

from app.ai.result import AnalysisFindings, extract_findings

BODY = "Summary:\n- thing happened\n\nSeverity assessment:\n- looks right"


def with_block(block: str) -> str:
    return f"{BODY}\n\n{block}"


def test_a_valid_block_is_extracted_and_stripped_from_the_text():
    answer = with_block('{"suggested_severity": "major", "affected_site_ids": [1007, 12], "confidence": 0.8}')
    text, findings = extract_findings(answer)
    assert text == BODY
    assert findings == AnalysisFindings("major", (1007, 12), 0.8)


def test_an_empty_affected_site_ids_list_is_valid():
    answer = with_block('{"suggested_severity": "cosmetic", "affected_site_ids": [], "confidence": 0.0}')
    text, findings = extract_findings(answer)
    assert text == BODY
    assert findings == AnalysisFindings("cosmetic", (), 0.0)


def test_no_block_at_all_leaves_the_answer_completely_unchanged():
    text, findings = extract_findings(BODY)
    assert text == BODY and findings is None


@pytest.mark.parametrize("block", [
    '{"suggested_severity": "catastrophic", "affected_site_ids": [1007], "confidence": 0.5}',  # not an allowed value
    '{"suggested_severity": 3, "affected_site_ids": [1007], "confidence": 0.5}',                # wrong type
    '{"affected_site_ids": [1007], "confidence": 0.5}',                                        # missing severity
    '{"suggested_severity": "major", "confidence": 0.5}',                                      # missing site ids
    '{"suggested_severity": "major", "affected_site_ids": "1007", "confidence": 0.5}',          # not a list
    '{"suggested_severity": "major", "affected_site_ids": ["1007"], "confidence": 0.5}',        # ids not integers
    '{"suggested_severity": "major", "affected_site_ids": [true], "confidence": 0.5}',          # bool, not an int
    '{"suggested_severity": "major", "affected_site_ids": [1007], "confidence": "high"}',       # not a number
    '{"suggested_severity": "major", "affected_site_ids": [1007], "confidence": true}',         # bool, not a number
    '{"suggested_severity": "major", "affected_site_ids": [1007], "confidence": 1.5}',          # out of range
    '{"suggested_severity": "major", "affected_site_ids": [1007], "confidence": -0.1}',         # out of range
    '{"suggested_severity": "major", "affected_site_ids": [1007]}',                             # missing confidence
    '{"suggested_severity": "major", "affected_site_ids": [1007], "confidence": 0.5,}',         # malformed JSON
])
def test_a_present_but_invalid_block_is_stripped_but_yields_no_findings(block):
    text, findings = extract_findings(with_block(block))
    assert text == BODY         # the (invalid) block is still removed from what gets posted
    assert findings is None


@pytest.mark.parametrize("tail", [
    "not json at all",
    "{not even a valid object",
    "",
])
def test_text_with_no_recognisable_block_is_returned_as_is(tail):
    answer = f"{BODY}\n\n{tail}" if tail else BODY
    text, findings = extract_findings(answer)
    assert text == answer and findings is None
