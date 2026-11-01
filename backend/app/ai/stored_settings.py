"""
AI settings chosen on the setup page and kept, encrypted, in the database
(app/settings_store.py, "ai"), by their .env names. setting(name) is how
every AI module reads a setting: the stored value when there is one, else
the environment (.env) - which keeps working as the course brief requires
(and is what `main.py --live` uses).

Once an installation runs (app/app_config.py's apply() calls saved_only()),
the provider, its models and Gemini's plan come ONLY from what is saved:
backend/.env no longer changes them. The API keys are the exception - a key
that is not saved is still read from the environment (the course brief:
the external service's key comes from environment variables). The other AI
settings (timeouts, log level...) are app settings, applied to the
environment at start by app/app_config.py.
"""

import os
import threading
from contextlib import contextmanager
from typing import Dict, Optional

_stored: Dict[str, str] = {}
_try_lock = threading.Lock()
_saved_only = False

# Never read from the environment once saved_only() is on: the choice is the app's.
CHOICE_NAMES = ("AI_PROVIDER", "GEMINI_TIER", "GEMINI_MODELS", "GEMINI_MODEL", "ANTHROPIC_MODEL", "OPENAI_MODEL")


def use(settings: Optional[dict]) -> None:
    """Replace the stored settings (called at startup and by the setup page)."""
    global _stored
    _stored = {str(k): str(v) for k, v in (settings or {}).items() if v not in (None, "")}


def saved_only(on: bool) -> None:
    """On: the AI choice (CHOICE_NAMES) comes only from the saved settings, never from .env."""
    global _saved_only
    _saved_only = on


def setting(name: str) -> Optional[str]:
    if name in _stored:
        return _stored[name]
    if _saved_only and name in CHOICE_NAMES:
        return None
    return os.environ.get(name)


def stored() -> Dict[str, str]:
    return dict(_stored)


@contextmanager
def trying(candidate: dict):
    """Read the settings as if `candidate` were stored, for the duration of the block (setup's checks)."""
    global _stored
    with _try_lock:
        saved = _stored
        use(candidate)
        try:
            yield
        finally:
            _stored = saved
