"""
Built-in system users: accounts the app itself uses to author comments.

Today there is one, `ai-assistant`, which posts AI analysis results (and
their error messages) on faults - whichever AI provider (Gemini, Claude or
OpenAI) is configured. Each answer names the provider and model in its own
text, so a single author is enough even if the provider changes later.

A system user:

- is always present in the UserStore, added by seed_users_from_env() in
  app/repository.py, whether or not the AI feature is configured. That way,
  comments it wrote earlier are restored with the right author after a
  restart (app/persistence.py looks authors up in the UserStore), instead of
  showing the "(removed)" placeholder.
- can never log in: it has no password, check_password() always returns
  False, and set_password() always refuses. The login endpoint
  (app/api/auth.py) therefore rejects it like any wrong password.
- has a reserved username: INCIDENT_BRIDGE_USERS may not define a user with
  the same name (compared case-insensitively) - the app refuses to start
  with a clear message instead.

How to use it elsewhere:

    from app.models.system_users import AI_USERNAME
    ai_user = state.users.get(AI_USERNAME)
    incident.add_comment(ai_user, "...")

To add another system user later, add its username to SYSTEM_USERNAMES;
nothing else needs to change.
"""

import uuid
from datetime import datetime, timezone

from app.models.enums import Role
from app.models.user import User

AI_USERNAME = "ai-assistant"

# Every reserved system username. seed_users_from_env() creates one
# SystemUser per entry and refuses provisioned users with these names.
SYSTEM_USERNAMES = (AI_USERNAME,)
DISPLAY_NAMES = {AI_USERNAME: "AI assistant"}      # how the app shows them (app/display_names.py)


class SystemUser(User):
    """
    A User that belongs to the app rather than a person. It behaves like a
    normal User wherever an author or actor is needed (comments, log
    entries), but has no password and so can never log in.
    """

    def __init__(self, username: str):
        # Deliberately does NOT call User.__init__: that would require a
        # password and run it through PasswordPolicy.
        self.id = str(uuid.uuid4())
        self.username = username
        self.role = Role.USER
        self._password_hash = ""
        self.must_change_password = False
        self.disabled = False
        self.session_version = 1
        self.created_at = datetime.now(timezone.utc)

    def check_password(self, plain_password) -> bool:
        """Always False: a system user can never log in."""
        return False

    def set_password(self, current_password, new_password) -> None:
        """Always refuses: a system user has no password to change."""
        raise PermissionError("System users have no password.")

    def reset_password(self, new_password) -> None:
        """Always refuses: a system user has no password to reset."""
        raise PermissionError("System users have no password.")

    def __repr__(self) -> str:
        return f"SystemUser(username={self.username!r})"

    def __str__(self) -> str:
        return f"{self.username} (SYSTEM)"


def is_reserved_username(username: str) -> bool:
    """True if `username` matches a system username, ignoring letter case."""
    return username.lower() in (name.lower() for name in SYSTEM_USERNAMES)


def build_system_users() -> list:
    """One fresh SystemUser per name in SYSTEM_USERNAMES."""
    return [SystemUser(name) for name in SYSTEM_USERNAMES]
