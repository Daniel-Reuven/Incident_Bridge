"""
Display names: how people are shown in the app, built from a template.

    {first-name} {last-name} - {job-title} ({location})

Placeholders: {first-name} {middle-name} {last-name} {job-title} {location}
{username}. One FORMAT for everyone (an admin sets it on the Users page;
kept in the encrypted settings, "display_name_format"), and an optional
per-user override (users.display_format). Usernames stay the identity:
logins, the activity log, the integrity checks and what is sent to the AI
provider use them; display names are only for the screen.

render() fills the template and tidies what empty fields leave behind -
"()" and "[]", a separator ( - , | ) at either end or doubled, extra spaces -
and falls back to the username when nothing is left. It returns the text
and its parts: the {location} part (with brackets or a separator that
belongs to it) is marked secondary, and the page draws it lighter.
"""

import re
from typing import Dict, List, Optional, Tuple

PLACEHOLDERS = ("first-name", "middle-name", "last-name", "job-title", "location", "username")
SECONDARY = {"location"}
DEFAULT_FORMAT = "{first-name} {last-name}"
PRESETS = (
    "{first-name} {last-name}",
    "{first-name} {middle-name} {last-name}",
    "{last-name}, {first-name}",
    "{first-name} {last-name} - {job-title}",
    "{first-name} {last-name} - {job-title} ({location})",
    "{first-name} {last-name} ({location})",
    "{username}",
)
MAX_FORMAT = 120
MAX_FIELD = 60
NAME_FIELDS = ("first_name", "middle_name", "last_name", "job_title", "location")

_TOKEN = re.compile(r"\{([^{}]*)\}")
_SEPARATORS = (" - ", ", ", " | ", " · ")
_OPEN, _CLOSE = "([", ")]"

_default_format = DEFAULT_FORMAT


def validate_format(template: str) -> str:
    """The template, stripped; ValueError for an unknown placeholder, no placeholder at all, or too long."""
    template = (template or "").strip()
    if not template:
        raise ValueError("The display name format is empty.")
    if len(template) > MAX_FORMAT:
        raise ValueError(f"The display name format is too long (at most {MAX_FORMAT} characters).")
    names = _TOKEN.findall(template)
    unknown = [name for name in names if name not in PLACEHOLDERS]
    if unknown:
        raise ValueError(f"Unknown placeholder(s): {', '.join('{' + n + '}' for n in unknown)} - use "
                         f"{' '.join('{' + p + '}' for p in PLACEHOLDERS)}.")
    if not names:
        raise ValueError("The display name format needs at least one placeholder, e.g. {first-name}.")
    return template


def set_default_format(template: Optional[str]) -> None:
    global _default_format
    _default_format = template or DEFAULT_FORMAT


def default_format() -> str:
    return _default_format


def render(template: str, fields: Dict[str, str], username: str) -> Tuple[str, List[Tuple[str, bool]]]:
    """(text, [(part, secondary)]) - see the module docstring."""
    values = {name: (fields.get(name.replace("-", "_")) or "").strip() for name in PLACEHOLDERS}
    values["username"] = username
    tokens: List[list] = []                       # [text, secondary, is_value]
    position = 0
    for match in _TOKEN.finditer(template):
        if match.start() > position:
            tokens.append([template[position:match.start()], False, False])
        name = match.group(1)
        tokens.append([values.get(name, ""), name in SECONDARY, True])
        position = match.end()
    if position < len(template):
        tokens.append([template[position:], False, False])

    # An empty value takes its brackets with it: "({location})" with no location leaves nothing.
    for index, token in enumerate(tokens):
        if not token[2] or token[0]:
            continue
        before = tokens[index - 1] if index > 0 and not tokens[index - 1][2] else None
        after = tokens[index + 1] if index + 1 < len(tokens) and not tokens[index + 1][2] else None
        if before and after and _opens(before[0]) and _closes(after[0]):
            before[0] = before[0].rstrip()[:-1]
            after[0] = after[0].lstrip()[1:]

    # A secondary value takes its brackets, or the separator just before it, into its part.
    for index, token in enumerate(tokens):
        if not (token[2] and token[1] and token[0]):
            continue
        before = tokens[index - 1] if index > 0 and not tokens[index - 1][2] else None
        after = tokens[index + 1] if index + 1 < len(tokens) and not tokens[index + 1][2] else None
        if before and after and _opens(before[0]) and _closes(after[0]):
            token[0] = before[0].rstrip()[-1] + token[0] + after[0].lstrip()[0]
            before[0] = before[0].rstrip()[:-1] + (" " if before[0].rstrip()[:-1] else "")
            after[0] = after[0].lstrip()[1:]

    # Join neighbours of the same weight, then tidy the separators and spaces left by empty values.
    parts: List[List] = []
    for text, secondary, _ in tokens:
        if not text:
            continue
        if parts and parts[-1][1] == secondary:
            parts[-1][0] += text
        else:
            parts.append([text, secondary])
    text = "".join(p[0] for p in parts)
    tidy = _tidy(text)
    if not tidy.strip(" -,|·()[]"):
        return username, [(username, False)]
    return tidy, _realign(parts, tidy)


def _opens(literal: str) -> bool:
    last = literal.rstrip()[-1:]
    return bool(last) and last in _OPEN


def _closes(literal: str) -> bool:
    first = literal.lstrip()[:1]
    return bool(first) and first in _CLOSE


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\s+[-,|·]\s*(?=[(\[])", " ", text)      # "Dana - (Haifa)" -> "Dana (Haifa)"
    text = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):
        for sep in _SEPARATORS:
            core = sep.strip()
            text = re.sub(rf"(\s*{re.escape(core)}\s*){{2,}}", sep, text)
            if text.startswith(core):
                text = text[len(core):].lstrip()
            if text.endswith(core):
                text = text[:-len(core)].rstrip()
    return text.strip()


def _realign(parts: List[List], tidy: str) -> List[Tuple[str, bool]]:
    """The tidied text cut back into its weighted parts (secondary parts are kept whole when still present)."""
    result: List[Tuple[str, bool]] = []
    rest = tidy
    for index, (text, secondary) in enumerate(parts):
        if not secondary:
            continue
        found = rest.find(text.strip())
        if found < 0:
            continue
        if found:
            result.append((rest[:found], False))
        result.append((text.strip(), True))
        rest = rest[found + len(text.strip()):]
    if rest:
        result.append((rest, False))
    return [(t, s) for t, s in result if t] or [(tidy, False)]


def display_of(user) -> Tuple[str, List[Tuple[str, bool]]]:
    """(text, parts) for a User - its own format, else the default one (a system user: its fixed name)."""
    from app.models.system_users import DISPLAY_NAMES
    if user.username in DISPLAY_NAMES:
        return DISPLAY_NAMES[user.username], [(DISPLAY_NAMES[user.username], False)]
    fields = {name: getattr(user, name, "") or "" for name in NAME_FIELDS}
    return render(getattr(user, "display_format", None) or _default_format, fields, user.username)
