"""
Tests for fail-fast user provisioning: User.from_dict (app/models/user.py),
UserStore's duplicate rule and seed_users_from_env (app/repository.py - the
regular users the setup page creates), and serve_app.py refusing to start
with a bad INCIDENT_BRIDGE_USERS or a database without its keys file.

Run from backend/:  python -m pytest tests/test_user_provisioning.py -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Role, User
from app.repository import UserProvisioningError, UserStore, seed_users_from_env
from tests.conftest import env_without_dotenv

ADMIN = {"username": "admin", "password": "Passw0rd!1", "role": "admin"}
TECH = {"username": "tech1", "password": "Passw0rd!2", "role": "user"}


def set_users(monkeypatch, value):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", value if isinstance(value, str) else json.dumps(value))


# --- User.from_dict -----------------------------------------------------------

def test_from_dict_builds_a_user():
    user = User.from_dict({"username": "  tech1 ", "password": "Passw0rd!2", "role": "user"})
    assert (user.username, user.role) == ("tech1", Role.USER) and user.check_password("Passw0rd!2")


@pytest.mark.parametrize("entry, message", [
    ("tech1", "JSON object"),
    ({"username": "tech1", "role": "user"}, "missing field\\(s\\): password"),
    ({"password": "Passw0rd!2"}, "missing field\\(s\\): username, role"),
    ({"username": " ", "password": "Passw0rd!2", "role": "user"}, "non-blank"),
    ({"username": "tech1", "password": "Passw0rd!2", "role": "boss"}, "must be one of: admin, user"),
    ({"username": "tech1", "password": 12345678, "role": "user"}, "must be a string"),
    ({"username": "tech1", "password": "short", "role": "user"}, "at least 8"),
])
def test_from_dict_rejects_bad_entries(entry, message):
    with pytest.raises(ValueError, match=message):
        User.from_dict(entry)


# --- UserStore ------------------------------------------------------------------

def test_store_rejects_duplicates_case_insensitively():
    store = UserStore()
    store.add(User("tech1", Role.USER, "Passw0rd!2"))
    with pytest.raises(ValueError, match="already used by 'tech1'"):
        store.add(User("TECH1", Role.ADMIN, "Passw0rd!1"))
    assert store.get("tech1").role == Role.USER          # the original was not replaced
    assert store.get("TECH1") is None                    # lookups stay exact-case


# --- seed_users_from_env (regular users only - admins come from the setup page) -------

TECH2 = {"username": "tech2", "password": "Passw0rd!3", "role": "user"}


def test_valid_value_loads_every_user(monkeypatch):
    set_users(monkeypatch, [TECH, TECH2])
    store = seed_users_from_env()
    # The built-in "ai-assistant" system user is always added (see app/models/system_users.py).
    assert {u.username for u in store.all_users()} == {"tech1", "tech2", "ai-assistant"}


def test_an_admin_entry_is_refused(monkeypatch):
    set_users(monkeypatch, [ADMIN, TECH])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    assert "entry 1: 'admin' is an admin - admins are not created from INCIDENT_BRIDGE_USERS" in str(caught.value)


def test_duplicate_username_stops_startup_with_a_clear_message(monkeypatch):
    set_users(monkeypatch, [TECH, TECH2, {"username": "Tech1", "password": "Passw0rd!3", "role": "user"}])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    message = str(caught.value)
    assert "INCIDENT_BRIDGE_USERS has 1 problem(s)" in message
    assert "entry 3: duplicate username 'Tech1' (already defined in entry 1)" in message
    assert "Fix the value in backend/.env" in message


def test_every_problem_is_reported_at_once(monkeypatch):
    set_users(monkeypatch, [TECH, TECH, {"username": "x", "role": "user"}, {"username": "y", "password": "Passw0rd!1",
                                                                            "role": "boss"}])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    message = str(caught.value)
    assert "has 3 problem(s)" in message
    assert "entry 2: duplicate username 'tech1'" in message
    assert "entry 3: missing field(s): password" in message
    assert "entry 4: 'role' must be one of" in message


@pytest.mark.parametrize("name", ["ai-assistant", "AI-Assistant", "AI-ASSISTANT"])
def test_a_reserved_system_username_stops_startup(monkeypatch, name):
    set_users(monkeypatch, [TECH, {"username": name, "password": "Passw0rd!3", "role": "user"}])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    assert f"entry 2: username {name!r} is reserved for a built-in system user" in str(caught.value)


@pytest.mark.parametrize("value, message", [
    ("[{'username': 'admin'}]", "not valid JSON"),
    ('{"username": "admin"}', "must be a JSON array"),
])
def test_unusable_values(monkeypatch, value, message):
    set_users(monkeypatch, value)
    with pytest.raises(UserProvisioningError, match=message):
        seed_users_from_env()


@pytest.mark.parametrize("value", [None, "", "   ", "[]"])
def test_unset_blank_or_empty_means_no_regular_users(monkeypatch, app_log, value):
    if value is None:
        monkeypatch.delenv("INCIDENT_BRIDGE_USERS", raising=False)
    else:
        set_users(monkeypatch, value)
    store = seed_users_from_env()
    assert {u.username for u in store.all_users()} == {"ai-assistant"}     # no demo accounts any more
    assert app_log.records == []


# --- serve_app.py -----------------------------------------------------------------

def test_serve_app_exits_with_the_message_and_no_traceback(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    # Every setting is set (backend/.env never applies - tests/conftest.py); a new installation, so the users are checked.
    env = env_without_dotenv(INCIDENT_BRIDGE_USERS=json.dumps([TECH, TECH]), DATABASE_PATH=str(tmp_path / "t.db"))
    result = subprocess.run([sys.executable, "serve_app.py"], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "duplicate username 'tech1'" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_app_refuses_a_database_without_its_keys_file(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    (tmp_path / "t.db").write_bytes(b"not a database")
    env = env_without_dotenv(DATABASE_PATH=str(tmp_path / "t.db"))
    result = subprocess.run([sys.executable, "serve_app.py"], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "has no keys file" in result.stderr and "manage_keys.py restore" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_app_refuses_a_keys_file_from_another_database(tmp_path, monkeypatch):
    """A keys file and a database that do not belong together: the message, not a traceback."""
    from app.security.installation import Installation
    backend = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "a" / "ib.db"))
    installation = Installation.from_env()
    _, key, data = installation.create()
    installation.save_new(key, data)                            # a real keys file (installation "a")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "ib.db").write_bytes(b"not this installation's database")
    env = env_without_dotenv(DATABASE_PATH=str(tmp_path / "b" / "ib.db"),
                             DATABASE_KEYS_PATH=str(tmp_path / "a" / "ib.db.keys"))
    result = subprocess.run([sys.executable, "serve_app.py"], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "does not open with the key from" in result.stderr
    assert "Traceback" not in result.stderr and "sqlite3Codec" not in result.stderr
