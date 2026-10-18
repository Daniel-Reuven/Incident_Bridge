"""
Admin-only user management (the Users page, frontend/users.html).

    GET   /users                               every account (no system users, never a hash)
    POST  /users                               create {username, role, password, confirm_password}
    PATCH /users/{username}                    change {role} and/or {disabled}
    POST  /users/{username}/reset-password     {new_password, confirm_password}

Rules:
  - usernames: 3-32 characters, letters, digits, ".", "_" or "-"; unique
    ignoring letter case; not a reserved system name ("ai-assistant");
  - a new account and a reset password must be changed at the user's next
    login (must_change_password), and a reset ends the user's sessions;
  - accounts are disabled, never deleted - incidents and comments keep
    pointing at their author;
  - an admin cannot change their own role or disable themselves - so the
    admin making a change always stays an enabled admin, and the app can
    never be left without one.
"""

import re

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_state, get_user_admin
from app.api.schemas import CreateUserRequest, ResetPasswordRequest, UpdateUserRequest
from app.api.serializers import account_to_dict
from app.audit import audit
from app.models import Role, User
from app.models.system_users import is_reserved_username
from app.repository import first_login_change_required
from app.state import AppState

router = APIRouter(prefix="/users", tags=["users"])

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise ValueError(f"role must be one of: {', '.join(r.value for r in Role)}") from None


def _account(state: AppState, username: str) -> User:
    user = state.users.get(username)
    if user is None or is_reserved_username(username):
        raise HTTPException(status_code=404, detail=f"No user named {username!r}.")
    return user


def _check_confirmation(password: str, confirmation: str) -> None:
    if password != confirmation:
        raise ValueError("The password and its confirmation do not match.")


@router.get("")
def list_users(state: AppState = Depends(get_state), _admin: User = Depends(get_user_admin)):
    return [account_to_dict(u) for u in state.users.accounts()]


@router.post("", status_code=201)
def create_user(body: CreateUserRequest, state: AppState = Depends(get_state),
                admin: User = Depends(get_user_admin)):
    username = body.username.strip()
    if not USERNAME_PATTERN.match(username):
        raise ValueError("Username must be 3-32 characters: letters, digits, '.', '_' or '-'.")
    if is_reserved_username(username):
        raise ValueError(f"{username!r} is reserved for a built-in system user.")
    if state.users.find_case_insensitive(username) is not None:
        raise RuntimeError(f"The username {username!r} is already taken.")          # 409
    _check_confirmation(body.password, body.confirm_password)
    user = User(username, _role(body.role), body.password, must_change_password=first_login_change_required())
    state.users.add(user)
    audit("users.created", user=admin.username, target=user.username, role=user.role.value,
          message=f"{admin.username} created {user.role.value} account {user.username}")
    return account_to_dict(user)


@router.patch("/{username}")
def update_user(username: str, body: UpdateUserRequest, state: AppState = Depends(get_state),
                admin: User = Depends(get_user_admin)):
    user = _account(state, username)
    new_role = _role(body.role) if body.role is not None else user.role
    disable = body.disabled if body.disabled is not None else user.disabled
    if user is admin and (new_role != user.role or disable):
        raise PermissionError("You cannot change your own role or disable your own account.")
    changes = {}
    if new_role != user.role:
        changes["role"] = new_role.value
    if disable != user.disabled:
        changes["disabled"] = disable
    user.role = new_role
    user.set_disabled(disable)
    state.users.save(user)
    if changes:
        described = ", ".join(f"{key} -> {value}" for key, value in changes.items())
        audit("users.updated", user=admin.username, target=user.username, **changes,
              message=f"{admin.username} changed {user.username}: {described}")
    return account_to_dict(user)


@router.post("/{username}/reset-password")
def reset_password(username: str, body: ResetPasswordRequest, state: AppState = Depends(get_state),
                   admin: User = Depends(get_user_admin)):
    """Set a new password; their sessions end, and they must change it at their next login
    unless REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN=false."""
    user = _account(state, username)
    _check_confirmation(body.new_password, body.confirm_password)
    user.reset_password(body.new_password, require_change=first_login_change_required())
    state.users.save(user)
    audit("users.password_reset", user=admin.username, target=user.username,
          message=f"{admin.username} reset the password of {user.username}")
    return account_to_dict(user)
