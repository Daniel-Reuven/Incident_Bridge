"""
Encryption of the activity log files (app/audit.py writes, app/log_reader.py
reads), with a key derived from the database key (HKDF) - so the log needs
no key of its own, the recovery key opens it too, and a copied log folder
is unreadable.

    current month   one line per record, each encrypted on its own (AES-256-GCM),
                    so the file can be appended to:
                        {"v": 1, "n": "<nonce>", "c": "<ciphertext>"}
                    the month is the authenticated data, so a line moved to
                    another month's file does not decrypt
    finished month  the month's plain lines zipped (deflate, best compression),
                    THEN encrypted as one blob: incident_bridge-YYYY-MM.zip.enc
                    (compressing first, because encrypted text does not compress)

LogCipher.for_database() is the installation's cipher (the database key
must be unlocked - app/database.py).
"""

import base64
import json
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ARCHIVE_MAGIC = b"IBLOG1"


class LogDecryptError(ValueError):
    """A line or archive that does not decrypt: changed, from another installation, or not encrypted."""


class LogCipher:
    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    @classmethod
    def from_database_key(cls, database_key: bytes) -> "LogCipher":
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"incident-bridge activity log v1").derive(database_key)
        return cls(key)

    @classmethod
    def for_database(cls) -> Optional["LogCipher"]:
        """The installation's log cipher, or None while the database key is not unlocked."""
        from app import database
        key = database.current_key()
        return cls.from_database_key(key) if key else None

    # --- lines -------------------------------------------------------------------

    def encrypt_line(self, text: str, month: str) -> str:
        nonce = os.urandom(12)
        sealed = self._aes.encrypt(nonce, text.encode("utf-8"), month.encode())
        return json.dumps({"v": 1, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(sealed).decode()})

    def decrypt_line(self, line: str, month: str) -> str:
        try:
            envelope = json.loads(line)
            return self._aes.decrypt(base64.b64decode(envelope["n"]), base64.b64decode(envelope["c"]),
                                     month.encode()).decode("utf-8")
        except (InvalidTag, ValueError, KeyError, TypeError):
            raise LogDecryptError("this line does not decrypt") from None

    # --- archives ------------------------------------------------------------------

    def encrypt_archive(self, data: bytes, month: str) -> bytes:
        nonce = os.urandom(12)
        return ARCHIVE_MAGIC + nonce + self._aes.encrypt(nonce, data, b"archive|" + month.encode())

    def decrypt_archive(self, blob: bytes, month: str) -> bytes:
        if not blob.startswith(ARCHIVE_MAGIC):
            raise LogDecryptError("not an encrypted archive")
        body = blob[len(ARCHIVE_MAGIC):]
        try:
            return self._aes.decrypt(body[:12], body[12:], b"archive|" + month.encode())
        except InvalidTag:
            raise LogDecryptError("the archive does not decrypt") from None
