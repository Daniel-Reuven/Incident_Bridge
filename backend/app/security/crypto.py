"""
The small cryptographic building blocks of the installation's keys
(the `cryptography` package; nothing here is home-made cryptography):

    new_key()                     32 random bytes (a database key, a key-encryption key)
    wrap(kek, key, aad)           AES-256-GCM: encrypt `key` with `kek`; `aad` ties the result to
    unwrap(kek, wrapped, aad)     its slot, so a slot cannot be copied into another one
    new_recovery_key()            32 random bytes shown as 13 groups of 4 characters (Base32)
    parse_recovery_key(text)      back to bytes - spaces, dashes and letter case are forgiven
    database_key(recovery)        the database key, DERIVED from the recovery key (HKDF-SHA256) -
                                  so the recovery key plus the database file is always enough
                                  to open the data, even when the keys file and the machine are lost
    fingerprint(key)              8 hex digits that identify a key without revealing it
"""

import base64
import binascii
import hashlib
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32


class WrongKey(ValueError):
    """unwrap() failed: the key-encryption key is wrong, or the slot was changed."""


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def wrap(kek: bytes, key: bytes, aad: str) -> dict:
    nonce = os.urandom(12)
    return {"nonce": b64(nonce), "ciphertext": b64(AESGCM(kek).encrypt(nonce, key, aad.encode("utf-8")))}


def unwrap(kek: bytes, wrapped: dict, aad: str) -> bytes:
    try:
        return AESGCM(kek).decrypt(unb64(wrapped["nonce"]), unb64(wrapped["ciphertext"]), aad.encode("utf-8"))
    except (InvalidTag, KeyError, ValueError, TypeError):
        raise WrongKey("the key does not open this slot") from None


def new_recovery_key() -> str:
    text = base64.b32encode(new_key()).decode("ascii").rstrip("=")          # 52 characters
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_recovery_key(text: str) -> bytes:
    cleaned = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (ValueError, binascii.Error):
        raw = b""
    if len(raw) != KEY_BYTES:
        raise WrongKey("That is not a recovery key (13 groups of 4 letters and digits).")
    return raw


def database_key(recovery_key: bytes) -> bytes:
    """The database key from the recovery key's 256 random bits (HKDF; no slow derivation is needed)."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None,
                info=b"incident-bridge database key v1").derive(recovery_key)


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"incident-bridge-key-fingerprint|" + key).hexdigest()[:8]
