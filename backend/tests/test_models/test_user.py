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
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    assert "Passw0rd!1" not in user._password_hash
    # The stored value looks like "pbkdf2_sha256$<iterations>$<hex salt>$<hex digest>".
    scheme, iterations, salt, digest = user.password_hash.split("$")
    assert scheme == "pbkdf2_sha256" and int(iterations) == User.HASH_ITERATIONS and len(salt) == 32


def test_check_password_true_for_correct_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    assert user.check_password("Passw0rd!1") is True


def test_check_password_false_for_wrong_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    assert user.check_password("WrongPass1") is False


def test_check_password_false_for_none():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    assert user.check_password(None) is False


def test_creating_user_with_weak_password_raises():
    """User.__init__ runs the initial password through PasswordPolicy too, not just later changes."""
    with pytest.raises(ValueError):
        User(username="alice", role=Role.USER, password="weak")


def test_set_password_with_wrong_current_password_raises_permission_error():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    with pytest.raises(PermissionError):
        user.set_password(current_password="WrongOne1", new_password="NewPass#1")
    # The password must NOT have changed after a rejected attempt.
    assert user.check_password("Passw0rd!1") is True


def test_set_password_with_weak_new_password_raises_value_error():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    with pytest.raises(ValueError):
        user.set_password(current_password="Passw0rd!1", new_password="weak")
    # The password must NOT have changed after a rejected attempt.
    assert user.check_password("Passw0rd!1") is True


def test_set_password_success_replaces_the_old_password():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    user.set_password(current_password="Passw0rd!1", new_password="NewPass#99")
    assert user.check_password("Passw0rd!1") is False
    assert user.check_password("NewPass#99") is True


def test_two_users_with_the_same_password_have_different_hashes():
    """Each User generates its own random salt, so identical passwords must NOT produce identical hashes."""
    user1 = User(username="alice", role=Role.USER, password="Passw0rd!1")
    user2 = User(username="bob", role=Role.USER, password="Passw0rd!1")
    assert user1._password_hash != user2._password_hash

def test_user_str_representation():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    assert str(user) == "alice (USER)"


def test_the_real_iteration_count_follows_owasp_2023():
    """tests/conftest.py lowers HASH_ITERATIONS for speed; the class default must stay the real value."""
    import app.models.user as user_module
    source = open(user_module.__file__, encoding="utf-8").read()
    assert "HASH_ITERATIONS = 600_000" in source


def test_an_older_weaker_hash_is_flagged_and_rehashed(monkeypatch):
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    monkeypatch.setattr(User, "HASH_ITERATIONS", User.HASH_ITERATIONS * 2)
    assert user.needs_rehash() and user.check_password("Passw0rd!1")      # the old hash still verifies
    user.rehash("Passw0rd!1")
    assert not user.needs_rehash() and user.check_password("Passw0rd!1")


def test_changing_the_password_clears_the_forced_change_and_bumps_the_session_version():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1", must_change_password=True)
    user.set_password("Passw0rd!1", "NewPass#99")
    assert user.must_change_password is False and user.session_version == 2


def test_the_new_password_must_differ_from_the_current_one():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    with pytest.raises(ValueError, match="different from the current one"):
        user.set_password("Passw0rd!1", "Passw0rd!1")


def test_an_admin_reset_forces_a_change_and_ends_sessions():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    user.reset_password("Reset#Pass1")
    assert user.check_password("Reset#Pass1") and user.must_change_password and user.session_version == 2
    with pytest.raises(ValueError):
        user.reset_password("weak")


def test_disabling_blocks_login_and_ends_sessions():
    user = User(username="alice", role=Role.USER, password="Passw0rd!1")
    user.set_disabled(True)
    assert not user.can_log_in and user.session_version == 2
    user.set_disabled(False)
    assert user.can_log_in and user.session_version == 2
