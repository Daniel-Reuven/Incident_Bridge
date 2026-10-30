"""
AI settings chosen on the setup page and kept, encrypted, in the database
(app/settings_store.py, "ai"), by their .env names. setting(name) is how
every AI module reads a setting: the stored value when there is one, else
the environment (.env) - which keeps working as the course brief requires
(and is what `main.py --live` uses).
"""

import os
import threading
from contextlib import contextmanager
from typing import Dict, Optional

_stored: Dict[str, str] = {}
_try_lock = threading.Lock()


def use(settings: Optional[dict]) -> None:
    """Replace the stored settings (called at startup and by the setup page)."""
    global _stored
    _stored = {str(k): str(v) for k, v in (settings or {}).items() if v not in (None, "")}


def setting(name: str) -> Optional[str]:
    if name in _stored:
        return _stored[name]
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
