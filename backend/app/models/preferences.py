"""
A user's interface preferences, stored with their account (users table,
column "preferences", JSON) so they follow the person to any browser:

    theme          "system" | "light" | "dark"       (Settings > Interface)
    dark_level     "soft" | "medium" | "deep"         (Settings > Interface: how dark the dark scheme is)
    text_scale     "0.9" | "1" | "1.15" | "1.3"       (Settings > Interface)
    updates_order  "asc" | "desc"                     (incident page: updates oldest / newest first)

The browser keeps a copy only while someone is logged in (frontend/js/theme.js),
so the theme is drawn before the page appears; logging out deletes it.
"""

from typing import Dict

ALLOWED: Dict[str, tuple] = {
    "theme": ("system", "light", "dark"),
    "dark_level": ("soft", "medium", "deep"),
    "text_scale": ("0.9", "1", "1.15", "1.3"),
    "updates_order": ("asc", "desc"),
}
DEFAULTS: Dict[str, str] = {"theme": "system", "dark_level": "medium", "text_scale": "1", "updates_order": "asc"}


def validated(changes: dict) -> Dict[str, str]:
    """`changes` checked against ALLOWED (only the given keys). Raises ValueError naming every problem."""
    problems = []
    for key, value in changes.items():
        if key not in ALLOWED:
            problems.append(f"Unknown preference {key!r}.")
        elif value not in ALLOWED[key]:
            problems.append(f"{key} must be one of: {', '.join(ALLOWED[key])}.")
    if problems:
        raise ValueError(" ".join(problems))
    return dict(changes)


def from_stored(stored) -> Dict[str, str]:
    """DEFAULTS overlaid with the valid values of a stored dict (unknown keys and bad values are ignored)."""
    prefs = dict(DEFAULTS)
    if isinstance(stored, dict):
        prefs.update({k: v for k, v in stored.items() if k in ALLOWED and v in ALLOWED[k]})
    return prefs
