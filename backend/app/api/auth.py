"""
Auth endpoints: session-based login, logout, current user, the password
policy, and changing your own password. There is no self-registration:
accounts are created by an admin (app/api/users.py) - the very first ones
come from INCIDENT_BRIDGE_USERS when the users table is empty (see
app/repository.py's load_users()).

Login hardening:
  - failed logins are limited per username and per client address
    (app/services/login_guard.py) - 429 with Retry-After while locked;
  - an unknown or disabled username costs the same time as a wrong
    password (dummy_password_check), and gets the same answer, so neither
    the response nor its timing reveals which usernames exist;
  - a hash made with fewer PBKDF2 iterations than today's setting is
    re-hashed on a successful login (User.needs_rehash()).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_current_user, get_session_user, get_state, start_session
from app.api.schemas import ChangePasswordRequest, LoginRequest, PreferencesRequest
from app.api.serializers import user_to_dict
from app.audit import audit
from app.models import User
from app.models.user import dummy_password_check
from app.services.password_policy import PasswordPolicy
from app.state import AppState

router = APIRouter(prefix="/auth", tags=["auth"])

GUIDE_POLICY_URL = "/guide.html#password-policy"


def _client_address(request: Request):
    return request.client.host if request.client else None


@router.post("/login")
def login(body: LoginRequest, request: Request, state: AppState = Depends(get_state)):
    guard, address = state.login_guard, _client_address(request)
    wait = guard.retry_after(body.username, address)
    if wait:
        minutes = max(1, round(wait / 60))
        audit("auth.login_locked", level=logging.WARNING, user=body.username, ip=address,
              message=f"Login refused for {body.username!r}: locked for {wait} more seconds")
        raise HTTPException(status_code=429, headers={"Retry-After": str(wait)},
                            detail=f"Too many failed logins - try again in about {minutes} minute(s).")

    user = state.users.get(body.username)
    if user is None or not user.can_log_in:
        dummy_password_check(body.password)              # same time as a real check
        ok = False
    else:
        ok = user.check_password(body.password)
    if not ok:
        guard.record_failure(body.username, address)
        # The attempted username is recorded (it may not exist); the password never is.
        audit("auth.login_failed", level=logging.WARNING, user=body.username, ip=address,
              message=f"Failed login for {body.username!r}")
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    guard.record_success(body.username, address)
    if user.needs_rehash():
        user.rehash(body.password)
        state.users.save(user)
    start_session(request, user)
    audit("auth.login", user=user.username, ip=address, message=f"{user.username} logged in")
    return user_to_dict(user)


@router.post("/logout")
def logout(request: Request):
    username = request.session.get("username")
    request.session.clear()
    if username:
        audit("auth.logout", user=username, ip=_client_address(request), message=f"{username} logged out")
    return {"detail": "Logged out."}


@router.get("/me")
def me(current_user: User = Depends(get_session_user)):
    """The logged-in user, including must_change_password (the frontend then forces the Password tab)."""
    return user_to_dict(current_user)


@router.patch("/preferences")
def update_preferences(body: PreferencesRequest, state: AppState = Depends(get_state),
                       current_user: User = Depends(get_current_user)):
    """
    Save your interface preferences (theme, text_scale, updates_order - see
    app/models/preferences.py) with your account; only the given ones change.
    Returns all of them. An unknown value is 400.
    """
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    changed = current_user.update_preferences(changes)
    if changed:
        state.users.save(current_user)
        described = ", ".join(f"{key} -> {value}" for key, value in changed.items())
        audit("auth.preferences_changed", user=current_user.username, **changed,
              message=f"{current_user.username} changed their interface settings: {described}")
    return dict(current_user.preferences)


@router.get("/password-policy")
def password_policy():
    """The password rule as data - public, so the forms can show it before saving anything."""
    return {"min_length": PasswordPolicy.MIN_LENGTH, "max_length": PasswordPolicy.MAX_LENGTH,
            "rules": PasswordPolicy.rules(), "guide_url": GUIDE_POLICY_URL}


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, request: Request, state: AppState = Depends(get_state),
                    current_user: User = Depends(get_session_user)):
    """
    Change your own password: the current one, the new one, and the new one
    again (the confirmation is checked here too, not only in the browser).
    Every OTHER session of this user ends (session_version is bumped); this
    one is renewed and stays logged in. Also the way out of a forced change.
    """
    if body.new_password != body.confirm_password:
        raise ValueError("The new password and its confirmation do not match.")
    # User.set_password checks the current password and runs PasswordPolicy -
    # PermissionError/ValueError map to 403/400 in app/api/app.py.
    current_user.set_password(body.current_password, body.new_password)
    state.users.save(current_user)
    start_session(request, current_user)
    audit("auth.password_changed", user=current_user.username, ip=_client_address(request),
          message=f"{current_user.username} changed their password")
    return {"detail": "Password updated."}
