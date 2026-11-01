"""
Turns one AI answer (or one error) into ready-to-post comment texts.

Every comment starts with a header naming the provider and model, e.g.

    AI analysis by Gemini (model: gemini-x) - Part 1/2

    Summary:
    - ...

A short answer becomes one comment without the "Part" suffix. A longer one
is split on paragraph boundaries (blank lines) into at most MAX_PARTS
comments, each within MAX_COMMENT_LENGTH (app/models/comment.py). A single
paragraph that is too long on its own is split at line breaks, then at
spaces. If the answer still needs more than MAX_PARTS comments, the last one
is cut and ends with TRUNCATED_NOTE.
"""

from typing import List

from app.models.comment import MAX_COMMENT_LENGTH

MAX_PARTS = 3
TRUNCATED_NOTE = f"(Answer truncated: it was longer than {MAX_PARTS} comments.)"

# Room kept free in every comment for the header and the " - Part n/n" suffix.
_HEADER_ROOM = 200


def answer_header(provider_label: str) -> str:
    """e.g. 'AI analysis by Gemini (model: gemini-x)'. An absurdly long label is cut so the header always fits."""
    return f"AI analysis by {provider_label[:150]}"


def total_answer_budget() -> int:
    """How many characters of answer fit in MAX_PARTS comments - told to the model as its length limit."""
    return MAX_PARTS * (MAX_COMMENT_LENGTH - _HEADER_ROOM) - len(TRUNCATED_NOTE)


def split_answer(answer: str, provider_label: str) -> List[str]:
    """The comment texts for `answer`, in posting order (see the module docstring)."""
    body_limit = MAX_COMMENT_LENGTH - _HEADER_ROOM
    chunks = _pack(_paragraphs(answer, body_limit), body_limit)

    if len(chunks) > MAX_PARTS:
        chunks = chunks[:MAX_PARTS]
        room = body_limit - len(TRUNCATED_NOTE) - 2
        chunks[-1] = chunks[-1][:room].rstrip() + "\n\n" + TRUNCATED_NOTE

    header = answer_header(provider_label)
    if len(chunks) == 1:
        return [f"{header}\n\n{chunks[0]}"]
    return [f"{header} - Part {i}/{len(chunks)}\n\n{chunk}" for i, chunk in enumerate(chunks, start=1)]


def error_comment(provider_label: str, reason: str) -> str:
    """The comment posted when an analysis fails, e.g. '... failed: timed out after 180 seconds.'"""
    reason = " ".join(reason.split())          # one line
    room = MAX_COMMENT_LENGTH - _HEADER_ROOM
    if len(reason) > room:
        reason = reason[:room - 3] + "..."
    if not reason.endswith((".", "!", "?")):
        reason += "."
    return f"{answer_header(provider_label)} failed: {reason}"


def _paragraphs(text: str, limit: int) -> List[str]:
    """The answer's paragraphs, with any paragraph longer than `limit` broken into smaller pieces."""
    text = text.replace("\r\n", "\n").strip()
    pieces = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if paragraph:
            pieces.extend(_break_long(paragraph, limit))
    return pieces or ["(The model returned an empty answer.)"]


def _break_long(paragraph: str, limit: int) -> List[str]:
    """Split one paragraph into pieces of at most `limit` characters: at line breaks, else spaces, else anywhere."""
    pieces = []
    while len(paragraph) > limit:
        cut = paragraph.rfind("\n", 0, limit)
        if cut <= 0:
            cut = paragraph.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(paragraph[:cut].rstrip())
        paragraph = paragraph[cut:].lstrip()
    pieces.append(paragraph)
    return pieces


def _pack(paragraphs: List[str], limit: int) -> List[str]:
    """Join paragraphs (with blank lines) into as few chunks of at most `limit` characters as possible, in order."""
    chunks = [paragraphs[0]]
    for paragraph in paragraphs[1:]:
        if len(chunks[-1]) + 2 + len(paragraph) <= limit:
            chunks[-1] += "\n\n" + paragraph
        else:
            chunks.append(paragraph)
    return chunks
