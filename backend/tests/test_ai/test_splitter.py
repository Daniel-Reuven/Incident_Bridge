"""
Tests for turning an answer into comments (app/ai/splitter.py).

Run from backend/:  python -m pytest tests/test_ai/test_splitter.py -v
"""

from app.ai.splitter import MAX_PARTS, TRUNCATED_NOTE, error_comment, split_answer, total_answer_budget
from app.models.comment import MAX_COMMENT_LENGTH

LABEL = "Gemini (model: gemini-test)"


def paragraphs(count, size):
    return "\n\n".join(f"P{i} " + "y" * size for i in range(count))


def test_a_short_answer_is_one_comment_without_a_part_suffix():
    assert split_answer("Summary:\n- All good.", LABEL) == [
        "AI analysis by Gemini (model: gemini-test)\n\nSummary:\n- All good."]


def test_a_long_answer_is_split_on_paragraphs_with_part_headers():
    texts = split_answer(paragraphs(6, 1500), LABEL)
    assert len(texts) == 2
    assert texts[0].startswith("AI analysis by Gemini (model: gemini-test) - Part 1/2\n\n")
    assert texts[1].startswith("AI analysis by Gemini (model: gemini-test) - Part 2/2\n\n")
    assert all(len(text) <= MAX_COMMENT_LENGTH for text in texts)
    for text in texts:                       # no paragraph was cut in the middle
        body = text.split("\n\n", 1)[1]
        assert all(len(p) == 1500 + len("P0 ") for p in body.split("\n\n"))


def test_an_answer_within_the_budget_is_never_truncated():
    texts = split_answer(paragraphs(1000, 10)[:total_answer_budget()], LABEL)
    assert len(texts) <= MAX_PARTS and TRUNCATED_NOTE not in texts[-1]


def test_a_too_long_answer_is_cut_after_the_last_part():
    texts = split_answer(paragraphs(20, 1500), LABEL)
    assert len(texts) == MAX_PARTS
    assert texts[-1].endswith(TRUNCATED_NOTE)
    assert all(len(text) <= MAX_COMMENT_LENGTH for text in texts)


def test_one_huge_paragraph_is_broken_at_spaces():
    texts = split_answer(" ".join(["word"] * 2000), LABEL)
    assert len(texts) == 3 and all(len(text) <= MAX_COMMENT_LENGTH for text in texts)
    assert all("wor d" not in text for text in texts)


def test_an_error_becomes_one_line_naming_the_provider_and_model():
    assert error_comment(LABEL, "timed out after 180 seconds") == \
        "AI analysis by Gemini (model: gemini-test) failed: timed out after 180 seconds."
    assert len(error_comment(LABEL, "z" * 20_000)) <= MAX_COMMENT_LENGTH
