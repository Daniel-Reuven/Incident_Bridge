"""
Tests for user accounts in the database: app/persistence_users.py,
app/repository.py's load_users() (first start from INCIDENT_BRIDGE_USERS,
the table as the source of truth afterwards), backend/manage_users.py, the
login guard and the session settings.

Run from backend/:  python -m pytest tests/test_users_persistence.py -v
"""

import json
import sys
from pathlib import Path

import pytest

from app.api.security import session_settings_from_env
from app.models import Role
from app.persistence_users import SqliteUserStore
from app.repository import UserProvisioningError, load_users
from app.services.login_guard import LoginGuard

USERS = [{"username": "boss", "password": "Chief#Start1", "role": "admin"},
         {"username": "tech7", "password": "Tech#Start7", "role": "user"}]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps(USERS))
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "true")
    return str(tmp_path / "users.db")


def test_the_first_start_creates_the_env_users_who_must_change_their_password(db):
    users = load_users(SqliteUserStore(db))
    assert [u.username for u in users.accounts()] == ["boss", "tech7"]
    assert all(u.must_change_password for u in users.accounts())
    assert users.get("ai-assistant") is not None                       # system user, in memory only
    assert SqliteUserStore(db).count() == 2


def test_after_that_the_database_wins_over_the_env(db, monkeypatch, app_log):
    users = load_users(SqliteUserStore(db))
    boss = users.get("boss")
    boss.set_password("Chief#Start1", "Chief#Changed2")
    users.save(boss)
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "other", "password": "Oth#Pass12",
                                                             "role": "admin"}]))
    again = load_users(SqliteUserStore(db))                              # a restart
    assert [u.username for u in again.accounts()] == ["boss", "tech7"]
    assert again.get("boss").check_password("Chief#Changed2") and not again.get("boss").must_change_password
    assert "INCIDENT_BRIDGE_USERS is only used for the first start" in app_log.text


def test_a_bad_env_value_still_stops_the_first_start(db, monkeypatch):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "x1", "password": "weak", "role": "user"}]))
    with pytest.raises(UserProvisioningError, match="at least 8 characters"):
        load_users(SqliteUserStore(db))
    assert SqliteUserStore(db).count() == 0                             # nothing half-written


def test_the_forced_change_can_be_switched_off(db, monkeypatch):
    monkeypatch.setenv("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "false")
    assert not any(u.must_change_password for u in load_users(SqliteUserStore(db)).accounts())


def test_manage_users_resets_a_password(db, monkeypatch, capsys):
    load_users(SqliteUserStore(db))
    monkeypatch.setenv("DATABASE_PATH", db)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_users
    monkeypatch.setattr(manage_users.getpass, "getpass", lambda prompt="": "Fresh#Pass55")
    manage_users.main(["reset-password", "boss", "--no-change-required"])
    boss = next(u for u in SqliteUserStore(db).load_all() if u.username == "boss")
    assert boss.check_password("Fresh#Pass55") and not boss.must_change_password and boss.role == Role.ADMIN
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
