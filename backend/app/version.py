"""
The app's version number, from ONE place: backend/pyproject.toml.

app_version() reads the `version = "..."` line of pyproject.toml directly,
so a version bump shows up at the next restart, without reinstalling. (The
installed package's metadata, written by `pip install -e .`, keeps the
version of the last install - so it is only the fallback, for a copy of the
app that has no pyproject.toml next to it.) Bumping the version means
editing pyproject.toml only.

Used by:
    GET /health (app/api/app.py) -> {"status": "ok", "version": "0.4.8"}
    the FastAPI app's own version (shown in the API docs at /docs)
    the page footer and the About page (frontend/js/site-info.js reads /health)

Usage:
    from app.version import app_version
    app_version()   # "0.4.8"
"""

import re
from functools import lru_cache
from importlib import metadata
from pathlib import Path

DISTRIBUTION = "incident-bridge"           # [project] name in backend/pyproject.toml
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


@lru_cache(maxsize=1)
def app_version() -> str:
    """The version from pyproject.toml (via the installed metadata), or "unknown"."""
    from_file = _from_pyproject()
    if from_file:                           # a checkout: the file is the source of truth
        return from_file
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return "unknown"


def _from_pyproject() -> str:
    """The [project] version line of backend/pyproject.toml, or "" if the file is not there."""
    try:
        match = _VERSION_LINE.search(_PYPROJECT.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return match.group(1) if match else ""
