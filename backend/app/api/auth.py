"""
Auth endpoints: session-based login, logout, current-user, and
change-password. There is deliberately no registration endpoint - users
only come from pre-provisioning at startup (see app/repository.py's
seed_users_from_env), per the confirmed "no self-registration" decision.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import SESSION_USERNAME_KEY, get_current_user, get_state
from app.api.schemas import ChangePasswordRequest, LoginRequest
from app.api.serializers import user_to_dict
from app.models import User
from app.state import AppState

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, request: Request, state: AppState = Depends(get_state)):
    user = state.users.get(body.username)
    if user is None or not user.check_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    request.session[SESSION_USERNAME_KEY] = user.username
    return user_to_dict(user)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"detail": "Logged out."}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return user_to_dict(current_user)


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, current_user: User = Depends(get_current_user)):
    # User.set_password checks the current password and runs PasswordPolicy
    # itself (see app/models/user.py) - raises PermissionError/ValueError,
    # both mapped to the right HTTP status by app/api/app.py's handlers.
    current_user.set_password(body.current_password, body.new_password)
    return {"detail": "Password updated."}
