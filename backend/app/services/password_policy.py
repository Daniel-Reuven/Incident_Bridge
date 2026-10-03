"""Shared password validation rule (confirmed design decision - README section 8, #4)."""

import re


class PasswordPolicy:
    """
    Single shared validator, applied identically whether a password is
    set at user-provisioning time or changed later via User.set_password.
    Keeping this in one place (rather than duplicating checks in both
    call sites) is the point - the rule can only be enforced consistently
    if there's only one implementation of it.
    """

    MIN_LENGTH = 8
    _ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9]+$")

    @classmethod
    def validate(cls, password: "str | None") -> None:
        """Raise ValueError if password does not meet policy; otherwise return None."""
        if not password or len(password) < cls.MIN_LENGTH:
            raise ValueError(f"Password must be at least {cls.MIN_LENGTH} characters long.")
        if not cls._ALLOWED_PATTERN.match(password):
            raise ValueError("Password may only contain letters (A-Z, a-z) and digits (0-9).")

    def __str__(self) -> str:
        """Human-readable string representation of the password policy."""
        return f"PasswordPolicy(min_length={self.MIN_LENGTH}, allowed_chars='A-Z, a-z, 0-9')"

    def __repr__(self) -> str:
        """Unambiguous string representation for debugging."""
        return f"<PasswordPolicy min_length={self.MIN_LENGTH} pattern='{self._ALLOWED_PATTERN.pattern}'>"
