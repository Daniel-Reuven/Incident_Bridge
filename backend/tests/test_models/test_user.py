"""
Unit tests for app.models.user.User.

Focus areas: passwords are never stored in plain text, check_password
works correctly in both directions, and set_password enforces both
rules at once - "you must know the current password" AND "the new
password must pass PasswordPolicy" - without either one silently taking
effect when the other fails.
"""

import pytest

from app.models.enums import Role
from app.models.user import User


def test_password_is_not_stored_in_plain_text():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    assert "Passw0rd1" not in user._password_hash
    # The stored value should look like "<hex salt>:<hex digest>", not the raw password.
    assert ":" in user._password_hash


def test_check_password_true_for_correct_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    assert user.check_password("Passw0rd1") is True


def test_check_password_false_for_wrong_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    assert user.check_password("WrongPass1") is False


def test_check_password_false_for_none():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    assert user.check_password(None) is False


def test_creating_user_with_weak_password_raises():
    """User.__init__ runs the initial password through PasswordPolicy too, not just later changes."""
    with pytest.raises(ValueError):
        User(username="alice", role=Role.USER, password="weak")


def test_set_password_with_wrong_current_password_raises_permission_error():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    with pytest.raises(PermissionError):
        user.set_password(current_password="WrongOne1", new_password="NewPass1")
    # The password must NOT have changed after a rejected attempt.
    assert user.check_password("Passw0rd1") is True


def test_set_password_with_weak_new_password_raises_value_error():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    with pytest.raises(ValueError):
        user.set_password(current_password="Passw0rd1", new_password="weak")
    # The password must NOT have changed after a rejected attempt.
    assert user.check_password("Passw0rd1") is True


def test_set_password_success_replaces_the_old_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd1")
    user.set_password(current_password="Passw0rd1", new_password="NewPass99")
    assert user.check_password("Passw0rd1") is False
    assert user.check_password("NewPass99") is True


def test_two_users_with_the_same_password_have_different_hashes():
    """Each User generates its own random salt, so identical passwords must NOT produce identical hashes."""
    user1 = User(username="alice", role=Role.USER, password="Passw0rd1")
    user2 = User(username="bob", role=Role.USER, password="Passw0rd1")
    assert user1._password_hash != user2._password_hash
