"""
Tests for the built-in system users (app/models/system_users.py): the
"ai-assistant" user that will author AI analysis comments.

Run from backend/:  python -m pytest tests/test_models/test_system_users.py -v
"""

import pytest

from app.models import MaintenanceTask, Role
from app.models.system_users import (
    AI_USERNAME, SystemUser, build_system_users, is_reserved_username,
)
from app.state import AppState
from tests.install_helper import add_demo_users


def test_a_system_user_can_never_log_in():
    ai_user = SystemUser(AI_USERNAME)
    assert ai_user.role == Role.USER
    assert ai_user.check_password("") is False
    assert ai_user.check_password("anything") is False
    assert ai_user.check_password(None) is False


def test_a_system_user_password_cannot_be_set():
    with pytest.raises(PermissionError):
        SystemUser(AI_USERNAME).set_password(None, "Passw0rd!1")


@pytest.mark.parametrize("name, reserved", [("ai-assistant", True), ("AI-Assistant", True), ("tech1", False)])
def test_reserved_names_are_matched_case_insensitively(name, reserved):
    assert is_reserved_username(name) is reserved


def test_build_system_users_creates_the_ai_user():
    assert [user.username for user in build_system_users()] == [AI_USERNAME]


def test_the_app_always_has_the_ai_user(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "system_users.db"))
    state = add_demo_users(AppState.create())
    assert isinstance(state.users.get(AI_USERNAME), SystemUser)


def test_ai_comments_keep_their_author_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ai_restart.db"))
    state = add_demo_users(AppState.create())
    admin = state.users.get("admin")
    task = MaintenanceTask(title="A", description="d", created_by=admin)
    state.incidents.add(task)
    task.add_comment(state.users.get(AI_USERNAME), "Analysis text.")
    state.incidents.save(task)

    restarted = AppState.create()
    restored_author = restarted.incidents.get(task.id).comments[-1].author
    assert restored_author.username == AI_USERNAME
    assert isinstance(restored_author, SystemUser)     # the real system user, not a "(removed)" placeholder
