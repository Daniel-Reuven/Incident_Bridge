"""
The app's own secrets, generated at setup and kept in the encrypted
database (app/settings_store.py, "secrets") - never in .env:

    session_secret     signs the login cookie (app/api/security.py's SessionFromSecrets)
    log_signing_key    signs and chains the activity log (app/audit.py, app/audit_integrity.py);
                       also keys the hashed client addresses (app/ip_privacy.py)

load_or_create() runs at every start once the database is unlocked; until
then both are None (the setup page needs neither).
"""

import secrets
from typing import Optional

_session_secret: Optional[str] = None
_log_signing_key: Optional[bytes] = None


def load_or_create(store) -> None:
    """Read the secrets from `store`, creating (and saving) any that are missing."""
    global _session_secret, _log_signing_key
    saved = store.get("secrets") or {}
    changed = False
    for name in ("session_secret", "log_signing_key"):
        if not saved.get(name):
            saved[name] = secrets.token_hex(32)
            changed = True
    if changed:
        store.set("secrets", saved)
    _session_secret = saved["session_secret"]
    _log_signing_key = saved["log_signing_key"].encode("ascii")


def session_secret() -> Optional[str]:
    return _session_secret


def log_signing_key() -> Optional[bytes]:
    return _log_signing_key


def forget() -> None:
    global _session_secret, _log_signing_key
    _session_secret = _log_signing_key = None
