"""
Tests for user accounts in the database: app/persistence_users.py,
app/repository.py's load_users() and create_initial_users() (the setup
page's accounts; the table as the source of truth afterwards), backend/manage_users.py, the
login guard and the session settings.

Run from backend/:  python -m pytest tests/test_users_persistence.py -v
"""

import json
import sys
from pathlib import Path

import pytest

from app.api.security import session_settings_from_env
from app.models import Role, User
from app.persistence_users import SqliteUserStore
from app.repository import UserProvisioningError, create_initial_users, load_users
from app.services.login_guard import LoginGuard

USERS = [{"username": "tech7", "password": "Tech#Start7", "role": "user"},
         {"username": "tech8", "password": "Tech#Start8", "role": "user"}]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps(USERS))
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "true")
    return str(tmp_path / "users.db")


def boss():
    return User("boss", Role.ADMIN, "Chief#Start1")


def test_a_new_database_has_no_accounts_until_the_setup(db):
    users = load_users(SqliteUserStore(db))
    assert users.accounts() == [] and users.get("ai-assistant") is not None   # system user, in memory only


def test_the_setup_creates_the_admin_and_the_env_users_who_must_change_their_password(db):
    users = load_users(SqliteUserStore(db))
    created = create_initial_users(users, boss())
    assert [u.username for u in created] == ["tech7", "tech8"]
    accounts = {u.username: u for u in load_users(SqliteUserStore(db)).accounts()}       # a restart
    assert set(accounts) == {"boss", "tech7", "tech8"}
    assert not accounts["boss"].must_change_password                    # chose their own password
    assert accounts["tech7"].must_change_password and accounts["tech8"].must_change_password


def test_after_that_the_database_wins_over_the_env(db, monkeypatch):
    users = load_users(SqliteUserStore(db))
    create_initial_users(users, boss())
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "other", "password": "Oth#Pass12",
                                                             "role": "user"}]))
    again = load_users(SqliteUserStore(db))                              # a restart
    assert [u.username for u in again.accounts()] == ["boss", "tech7", "tech8"]


def test_a_bad_env_value_or_a_clash_with_the_admin_creates_nothing(db, monkeypatch):
    users = load_users(SqliteUserStore(db))
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "x1", "password": "weak", "role": "user"}]))
    with pytest.raises(UserProvisioningError, match="at least 8 characters"):
        create_initial_users(users, boss())
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "BOSS", "password": "Tech#Start7",
                                                             "role": "user"}]))
    with pytest.raises(UserProvisioningError, match="the admin's username"):
        create_initial_users(users, boss())
    assert SqliteUserStore(db).count() == 0                             # nothing half-written


def test_the_forced_change_can_be_switched_off(db, monkeypatch):
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "false")
    users = load_users(SqliteUserStore(db))
    assert not any(u.must_change_password for u in create_initial_users(users, boss()))


def test_manage_users_resets_a_password(db, monkeypatch, capsys):
    from app.security.installation import Installation
    monkeypatch.setenv("DATABASE_PATH", db)
    installation = Installation.from_env()
    _, key, data = installation.create()
    installation.save_new(key, data)                                     # an installed, encrypted database
    create_initial_users(load_users(SqliteUserStore(db)), boss())
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_users
    monkeypatch.setattr(manage_users.getpass, "getpass", lambda prompt="": "Fresh#Pass55")
    manage_users.main(["reset-password", "boss", "--no-change-required"])
    boss_now = next(u for u in SqliteUserStore(db).load_all() if u.username == "boss")
    assert boss_now.check_password("Fresh#Pass55") and not boss_now.must_change_password
    assert boss_now.role == Role.ADMIN
    manage_users.main(["list"])
    assert "boss" in capsys.readouterr().out


def test_the_login_guard_locks_and_unlocks():
    now = [0.0]
    guard = LoginGuard(username_limit=3, address_limit=10, window_seconds=60, lockout_seconds=120,
                       clock=lambda: now[0])
    for _ in range(3):
        guard.record_failure("Admin", "1.2.3.4")
    assert guard.retry_after("admin", "5.6.7.8") == 120                   # username, any letter case
    assert guard.retry_after("tech1", "1.2.3.4") == 0                     # the address is under its limit
    now[0] = 121
    assert guard.retry_after("admin", "1.2.3.4") == 0


def test_old_failures_leave_the_window():
    now = [0.0]
    guard = LoginGuard(username_limit=3, window_seconds=60, clock=lambda: now[0])
    guard.record_failure("admin", None)
    guard.record_failure("admin", None)
    now[0] = 61
    guard.record_failure("admin", None)
    assert guard.retry_after("admin", None) == 0


@pytest.mark.parametrize("hours, secure, expected", [
    (None, None, (43200, False)), ("2", "true", (7200, True)), ("0.5", "no", (1800, False)),
])
def test_session_settings(monkeypatch, hours, secure, expected):
    for name, value in (("SESSION_MAX_AGE_HOURS", hours), ("SESSION_COOKIE_SECURE", secure)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert session_settings_from_env() == expected


@pytest.mark.parametrize("name, value", [("SESSION_MAX_AGE_HOURS", "0"), ("SESSION_MAX_AGE_HOURS", "soon"),
                                         ("SESSION_COOKIE_SECURE", "maybe")])
def test_bad_session_settings_stop_startup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        session_settings_from_env()


def test_a_comment_author_with_no_account_becomes_a_placeholder_that_cannot_log_in():
    from app.persistence import _unknown_user
    ghost = _unknown_user("former-tech")
    assert ghost.username == "former-tech (removed)" and not ghost.can_log_in
    assert not ghost.check_password("anything")
