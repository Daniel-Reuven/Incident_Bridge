"""
Shared password policy (README.md section 13, #4) - the ONE place that
decides whether a password is acceptable, used by every way a password is
set: the first users created from INCIDENT_BRIDGE_USERS, a user changing
their own password, an admin creating a user or resetting a password, and
backend/manage_users.py.

The rule (a character-mix policy):
    - 8 to 128 characters;
    - at least one uppercase letter, one lowercase letter, one digit and
      one symbol (anything that is not a letter or a digit, including a
      space);
    - must not contain the username (ignoring letter case);
    - must not be on a short list of very common passwords
      (COMMON_PASSWORDS - all of them pass the character rules, which is
      why the list is still worth having);
    - when changing a password: must differ from the current one (that
      check is in User.set_password(), which knows the current hash).

rules() describes the same rule as data, served by GET /auth/password-policy
so the Settings and user-management screens show the live checklist from
the server's own definition rather than a copy of it.
"""

from typing import List, Optional


class PasswordPolicy:
    MIN_LENGTH = 8
    MAX_LENGTH = 128

    # Lower-cased. Each one satisfies the character rules on its own, so
    # only this list stops them.
    COMMON_PASSWORDS = frozenset({
        "password1!", "password1@", "password1#", "password123!", "p@ssw0rd", "p@ssw0rd1", "p@ssword1",
        "passw0rd!", "passw0rd1!", "welcome1!", "welcome123!", "qwerty1!", "qwerty123!", "admin123!",
        "admin@123", "changeme1!", "letmein1!", "iloveyou1!", "abc123!@#", "abcd1234!", "test123!",
        "summer2025!", "summer2026!", "winter2025!", "winter2026!", "spring2026!", "autumn2026!",
        "football1!", "monkey123!", "dragon123!", "trustno1!", "zaq12wsx!", "1qaz@wsx", "1q2w3e4r!",
        "qwer1234!", "asdf1234!", "aa123456!", "123qwe!@#", "incident1!", "incidentbridge1!",
    })

    @classmethod
    def problems(cls, password: Optional[str], username: Optional[str] = None) -> List[str]:
        """Every rule `password` breaks, as readable sentences (an empty list = acceptable)."""
        if not isinstance(password, str) or not password:
            return [f"Password must be at least {cls.MIN_LENGTH} characters long."]
        found = []
        if len(password) < cls.MIN_LENGTH:
            found.append(f"Password must be at least {cls.MIN_LENGTH} characters long.")
        if len(password) > cls.MAX_LENGTH:
            found.append(f"Password must be at most {cls.MAX_LENGTH} characters long.")
        if not any(c.isupper() for c in password):
            found.append("Password must contain an uppercase letter.")
        if not any(c.islower() for c in password):
            found.append("Password must contain a lowercase letter.")
        if not any(c.isdigit() for c in password):
            found.append("Password must contain a digit.")
        if all(c.isalnum() for c in password):
            found.append("Password must contain a symbol (for example ! @ # $ % or a space).")
        if username and username.strip() and username.strip().lower() in password.lower():
            found.append("Password must not contain the username.")
        if password.lower() in cls.COMMON_PASSWORDS:
            found.append("Password is too common - choose a less predictable one.")
        return found

    @classmethod
    def validate(cls, password: Optional[str], username: Optional[str] = None) -> None:
        """Raise ValueError naming every broken rule; return None for an acceptable password."""
        found = cls.problems(password, username)
        if found:
            raise ValueError(" ".join(found))

    @classmethod
    def rules(cls) -> List[dict]:
        """The rule as data for the frontend checklist: [{"id", "text"}], in display order."""
        return [
            {"id": "length", "text": f"{cls.MIN_LENGTH} to {cls.MAX_LENGTH} characters"},
            {"id": "upper", "text": "An uppercase letter (A-Z)"},
            {"id": "lower", "text": "A lowercase letter (a-z)"},
            {"id": "digit", "text": "A digit (0-9)"},
            {"id": "symbol", "text": "A symbol, e.g. ! @ # $ % or a space"},
            {"id": "username", "text": "Does not contain your username"},
            {"id": "common", "text": "Not a common password"},
            {"id": "different", "text": "Different from your current password"},
        ]

    def __str__(self) -> str:
        return (f"PasswordPolicy({self.MIN_LENGTH}-{self.MAX_LENGTH} chars, upper + lower + digit + symbol, "
                f"no username, not common)")

    def __repr__(self) -> str:
        return (f"<PasswordPolicy min_length={self.MIN_LENGTH} max_length={self.MAX_LENGTH} "
                f"common_passwords={len(self.COMMON_PASSWORDS)}>")
