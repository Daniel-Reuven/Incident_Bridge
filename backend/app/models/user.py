"""User model: identity, role, and password handling."""

import hashlib
import os
import uuid

from app.models.enums import Role


class User:
    """
    A pre-provisioned user (no self-registration - see README section 6).

    Passwords are never stored in plain text. `set_password` always runs
    the new password through PasswordPolicy before accepting it, so the
    rule (min 8 chars, A-Z/a-z/0-9 only) is enforced in exactly one place
    and can't be bypassed by a caller forgetting to validate first.
    """

    # PBKDF2 iteration count. Deliberately high but still fast enough for
    # a course project; a production system should also consider a
    # dedicated library such as passlib or argon2-cffi.
    _HASH_ITERATIONS = 100_000

    def __init__(self, username: str, role: Role, password: str):
        self.id = str(uuid.uuid4())
        self.username = username
        self.role = role
        self._password_hash: str = ""
        # _is_provisioning=True skips the "current password" check below,
        # since there is no existing password yet at creation time.
        self.set_password(current_password=None, new_password=password, _is_provisioning=True)

    def set_password(self, current_password: "str | None", new_password: str,
                      _is_provisioning: bool = False) -> None:
        """
        Change the password. Requires the current password to match,
        except during initial provisioning (see __init__). Raises
        ValueError if the new password fails PasswordPolicy, or
        PermissionError if current_password is wrong.

        The PasswordPolicy import is local (not at module top) to avoid a
        circular import: app.services.password_policy doesn't depend on
        app.models, but keeping the import here makes that independence
        explicit rather than relying on import-order luck.
        """
        from app.services.password_policy import PasswordPolicy

        if not _is_provisioning and not self.check_password(current_password):
            raise PermissionError("Current password is incorrect.")

        PasswordPolicy.validate(new_password)

        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", new_password.encode("utf-8"), salt, self._HASH_ITERATIONS)
        self._password_hash = f"{salt.hex()}:{digest.hex()}"

    def check_password(self, plain_password: "str | None") -> bool:
        """Return True if plain_password matches the stored hash."""
        if not self._password_hash or plain_password is None:
            return False
        salt_hex, digest_hex = self._password_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, self._HASH_ITERATIONS)
        return actual == expected

    def __repr__(self) -> str:
        return f"User(username={self.username!r}, role={self.role.name})"
