"""User model: identity, role, account state and password handling."""

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.models.enums import Role


class User:
    """
    A user account. Since v0.5.x users live in the database
    (app/persistence_users.py): the first ones are created from
    INCIDENT_BRIDGE_USERS when the users table is empty, and after that the
    table is the source of truth (see app/repository.py's load_users()).

    Passwords are never stored in plain text: PBKDF2-HMAC-SHA256 with a
    random 16-byte salt, stored as "pbkdf2_sha256$<iterations>$<salt>$<hash>"
    so the iteration count can be raised later - check_password() reads the
    stored count, and needs_rehash() tells the login endpoint to re-hash an
    older, weaker hash with the current count. Comparison is constant-time
    (hmac.compare_digest).

    Every way of setting a password runs it through PasswordPolicy (in
    app/services/password_policy.py), so the rule can't be bypassed.

    Account state:
        must_change_password  set for the first users and after an admin
                              reset; the API then refuses everything
                              except changing the password (app/api/deps.py)
        disabled              cannot log in; existing sessions end
        session_version       stored in the login cookie; bumped by a
                              password change/reset or disabling, which
                              ends every other session of this user
    """

    # PBKDF2 iterations for NEW hashes (OWASP's 2023 recommendation for
    # PBKDF2-HMAC-SHA256). Tests lower it in tests/conftest.py for speed.
    HASH_ITERATIONS = 600_000
    _HASH_SCHEME = "pbkdf2_sha256"

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        """
        Alternate constructor: build a user from one entry of the
        INCIDENT_BRIDGE_USERS JSON array, e.g.
            {"username": "tech1", "password": "Tech1-Start!", "role": "user"}
        (see seed_users_from_env() in app/repository.py, the only caller).

        Raises ValueError with a clear message if the entry is not an
        object, a field is missing, the username is invalid, the role is
        not "admin" or "user", or the password fails PasswordPolicy.
        """
        if not isinstance(data, dict):
            raise ValueError(f"must be a JSON object, got {type(data).__name__}")
        missing = [key for key in ("username", "password", "role") if key not in data]
        if missing:
            raise ValueError(f"missing field(s): {', '.join(missing)}")
        username = data["username"]
        if not isinstance(username, str) or not username.strip():
            raise ValueError("'username' must be a non-blank string")
        try:
            role = Role(data["role"])
        except ValueError:
            allowed = ", ".join(r.value for r in Role)
            raise ValueError(f"'role' must be one of: {allowed} (got {data['role']!r})") from None
        if not isinstance(data["password"], str):
            raise ValueError("'password' must be a string")
        return cls(username=username.strip(), role=role, password=data["password"])

    @classmethod
    def from_record(cls, username: str, role: Role, password_hash: str, must_change_password: bool = False,
                    disabled: bool = False, session_version: int = 1,
                    created_at: Optional[datetime] = None) -> "User":
        """A user read back from the database (app/persistence_users.py): the stored hash, no policy check."""
        user = cls.__new__(cls)
        user.id = str(uuid.uuid4())
        user.username = username
        user.role = role
        user._password_hash = password_hash
        user.must_change_password = must_change_password
        user.disabled = disabled
        user.session_version = session_version
        user.created_at = created_at or datetime.now(timezone.utc)
        return user

    def __init__(self, username: str, role: Role, password: str, must_change_password: bool = False):
        self.id = str(uuid.uuid4())
        self.username = username
        self.role = role
        self._password_hash: str = ""
        self.must_change_password = must_change_password
        self.disabled = False
        self.session_version = 1
        self.created_at = datetime.now(timezone.utc)
        _policy().validate(password, username)
        self._password_hash = self._hash(password)

    # --- passwords --------------------------------------------------------

    def set_password(self, current_password: Optional[str], new_password: str) -> None:
        """
        The user changes their own password. Raises PermissionError if
        `current_password` is wrong, ValueError if the new one fails
        PasswordPolicy or equals the current one. On success: the new hash,
        must_change_password cleared, session_version bumped (other
        sessions end; the caller re-issues the current one).
        """
        if not self.check_password(current_password):
            raise PermissionError("Current password is incorrect.")
        problems = _policy().problems(new_password, self.username)
        if new_password == current_password:
            problems.append("The new password must be different from the current one.")
        if problems:
            raise ValueError(" ".join(problems))
        self._password_hash = self._hash(new_password)
        self.must_change_password = False
        self.session_version += 1

    def reset_password(self, new_password: str) -> None:
        """
        An admin (or backend/manage_users.py) sets a new password without
        knowing the old one. The user must change it at the next login, and
        every existing session of theirs ends. Raises ValueError if it fails
        PasswordPolicy.
        """
        _policy().validate(new_password, self.username)
        self._password_hash = self._hash(new_password)
        self.must_change_password = True
        self.session_version += 1

    def check_password(self, plain_password: Optional[str]) -> bool:
        """True if `plain_password` matches the stored hash (constant-time comparison)."""
        if not self._password_hash or not isinstance(plain_password, str):
            return False
        iterations, salt, expected = self._parse_hash(self._password_hash)
        actual = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)

    def needs_rehash(self) -> bool:
        """True when the stored hash uses fewer iterations than HASH_ITERATIONS (re-hash at next login)."""
        return bool(self._password_hash) and self._parse_hash(self._password_hash)[0] < self.HASH_ITERATIONS

    def rehash(self, plain_password: str) -> None:
        """Store a fresh hash of the (already verified) password with the current iteration count."""
        self._password_hash = self._hash(plain_password)

    @property
    def password_hash(self) -> str:
        """The stored hash string - for the database only, never sent to a client."""
        return self._password_hash

    # --- account state ----------------------------------------------------

    def set_disabled(self, disabled: bool) -> None:
        """Disable (cannot log in, sessions end) or enable the account."""
        if disabled and not self.disabled:
            self.session_version += 1
        self.disabled = disabled

    @property
    def can_log_in(self) -> bool:
        return not self.disabled and bool(self._password_hash)

    # --- helpers ----------------------------------------------------------

    @classmethod
    def _hash(cls, password: str) -> str:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, cls.HASH_ITERATIONS)
        return f"{cls._HASH_SCHEME}${cls.HASH_ITERATIONS}${salt.hex()}${digest.hex()}"

    @classmethod
    def _parse_hash(cls, stored: str):
        """(iterations, salt, digest) of a stored hash."""
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != cls._HASH_SCHEME:
            raise ValueError(f"unknown password hash scheme {scheme!r}")
        return int(iterations), bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)

    def __repr__(self) -> str:
        flags = "".join([", disabled" if self.disabled else "",
                         ", must_change_password" if self.must_change_password else ""])
        return f"User(username={self.username!r}, role={self.role.name}{flags})"

    def __str__(self) -> str:
        return f"{self.username} ({self.role.name})"


def dummy_password_check(plain_password: Optional[str]) -> None:
    """
    Spend the same time as a real check_password() - called by the login
    endpoint for an unknown or disabled username, so the response time
    does not reveal which usernames exist.
    """
    hashlib.pbkdf2_hmac("sha256", (plain_password or "").encode("utf-8"), b"\0" * 16, User.HASH_ITERATIONS)



def _policy():
    """
    PasswordPolicy, imported on first use: importing app.services at module
    level would import app.services.availability, which imports app.models
    - a cycle while app.models is still loading.
    """
    from app.services.password_policy import PasswordPolicy
    return PasswordPolicy
