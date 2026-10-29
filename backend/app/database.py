"""
The one way the app opens its database: connect(), an SQLCipher connection
(the `sqlcipher3` package - SQLite with the whole file encrypted, AES-256,
and every page checked with an HMAC, so a byte changed in the file is
caught when it is read).

The 256-bit database key comes from the installation (app/security/
installation.py): unlocked once at startup (or created by the setup page)
and handed here with use_key(). Without it connect() refuses - there is no
unencrypted mode. Every store (incidents, users, sites, the AI queue, the
activity-log checkpoint, the settings) opens its own connection through
connect(), so they all read and write the same encrypted file.

`Row` and the exception classes are re-exported, so the stores never import
sqlite3 (whose files SQLCipher cannot read and vice versa).
"""

import threading
from typing import Optional

from sqlcipher3 import dbapi2 as _sqlcipher

Row = _sqlcipher.Row
Error = _sqlcipher.Error
IntegrityError = _sqlcipher.IntegrityError
DatabaseError = _sqlcipher.DatabaseError

_key: Optional[bytes] = None
_lock = threading.Lock()


class DatabaseLocked(RuntimeError):
    """connect() was called before the installation's key was unlocked (setup not finished)."""


def use_key(key: bytes) -> None:
    """The database key for every connection from now on (32 bytes)."""
    global _key
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("the database key must be 32 bytes")
    with _lock:
        _key = key


def forget_key() -> None:
    global _key
    with _lock:
        _key = None


def is_unlocked() -> bool:
    return _key is not None


def connect(db_path: str, check_same_thread: bool = False):
    """
    An open connection to `db_path` with the database key applied. Raises
    DatabaseLocked before the key is known, and DatabaseError ("file is not
    a database") when the file was encrypted with another key or is not an
    SQLCipher database at all.
    """
    with _lock:
        key = _key
    if key is None:
        raise DatabaseLocked("The database is locked - the setup is not finished (see /setup.html).")
    conn = _sqlcipher.connect(db_path, check_same_thread=check_same_thread)
    try:
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")       # a raw key: no passphrase derivation
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()   # fails now if the key is wrong
    except Exception:
        conn.close()
        raise
    return conn
