"""
Tests for fail-fast user provisioning: User.from_dict (app/models/user.py),
UserStore's duplicate rule and seed_users_from_env (app/repository.py), and
serve_app.py refusing to start with a bad INCIDENT_BRIDGE_USERS.

Run from backend/:  python -m pytest tests/test_user_provisioning.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Role, User
from app.repository import UserProvisioningError, UserStore, seed_users_from_env

ADMIN = {"username": "admin", "password": "Passw0rd1", "role": "admin"}
TECH = {"username": "tech1", "password": "Passw0rd2", "role": "user"}


def set_users(monkeypatch, value):
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", value if isinstance(value, str) else json.dumps(value))


# --- User.from_dict -----------------------------------------------------------

def test_from_dict_builds_a_user():
    user = User.from_dict({"username": "  tech1 ", "password": "Passw0rd2", "role": "user"})
    assert (user.username, user.role) == ("tech1", Role.USER) and user.check_password("Passw0rd2")


@pytest.mark.parametrize("entry, message", [
    ("tech1", "JSON object"),
    ({"username": "tech1", "role": "user"}, "missing field\\(s\\): password"),
    ({"password": "Passw0rd2"}, "missing field\\(s\\): username, role"),
    ({"username": " ", "password": "Passw0rd2", "role": "user"}, "non-blank"),
    ({"username": "tech1", "password": "Passw0rd2", "role": "boss"}, "must be one of: admin, user"),
    ({"username": "tech1", "password": 12345678, "role": "user"}, "must be a string"),
    ({"username": "tech1", "password": "short", "role": "user"}, "at least 8"),
])
def test_from_dict_rejects_bad_entries(entry, message):
    with pytest.raises(ValueError, match=message):
        User.from_dict(entry)


# --- UserStore ------------------------------------------------------------------

def test_store_rejects_duplicates_case_insensitively():
    store = UserStore()
    store.add(User("tech1", Role.USER, "Passw0rd2"))
    with pytest.raises(ValueError, match="already used by 'tech1'"):
        store.add(User("TECH1", Role.ADMIN, "Passw0rd1"))
    assert store.get("tech1").role == Role.USER          # the original was not replaced
    assert store.get("TECH1") is None                    # lookups stay exact-case


# --- seed_users_from_env ---------------------------------------------------------

def test_valid_value_loads_every_user(monkeypatch):
    set_users(monkeypatch, [ADMIN, TECH])
    store = seed_users_from_env()
    assert {u.username for u in store.all_users()} == {"admin", "tech1"}


def test_duplicate_username_stops_startup_with_a_clear_message(monkeypatch):
    set_users(monkeypatch, [ADMIN, TECH, {"username": "Tech1", "password": "Passw0rd3", "role": "user"}])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    message = str(caught.value)
    assert "INCIDENT_BRIDGE_USERS has 1 problem(s)" in message
    assert "entry 3: duplicate username 'Tech1' (already defined in entry 2)" in message
    assert "Fix the value in backend/.env" in message


def test_every_problem_is_reported_at_once(monkeypatch):
    set_users(monkeypatch, [ADMIN, ADMIN, {"username": "x", "role": "user"}, {"username": "y", "password": "Passw0rd1",
                                                                              "role": "boss"}])
    with pytest.raises(UserProvisioningError) as caught:
        seed_users_from_env()
    message = str(caught.value)
    assert "has 3 problem(s)" in message
    assert "entry 2: duplicate username 'admin'" in message
    assert "entry 3: missing field(s): password" in message
    assert "entry 4: 'role' must be one of" in message


@pytest.mark.parametrize("value, message", [
    ("[{'username': 'admin'}]", "not valid JSON"),
    ('{"username": "admin"}', "must be a JSON array"),
    ("[]", "the array is empty"),
])
def test_unusable_values(monkeypatch, value, message):
    set_users(monkeypatch, value)
    with pytest.raises(UserProvisioningError, match=message):
        seed_users_from_env()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_falls_back_to_the_demo_pair(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("INCIDENT_BRIDGE_USERS", raising=False)
    else:
        set_users(monkeypatch, value)
    store = seed_users_from_env()
    assert {u.username for u in store.all_users()} == {"admin", "tech1"}
    assert "WARNING" in capsys.readouterr().out


def test_warning_can_be_silenced(monkeypatch, capsys):
    monkeypatch.delenv("INCIDENT_BRIDGE_USERS", raising=False)
    seed_users_from_env(warn=False)
    assert capsys.readouterr().out == ""


# --- serve_app.py -----------------------------------------------------------------

def test_serve_app_exits_with_the_message_and_no_traceback(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "INCIDENT_BRIDGE_USERS": json.dumps([ADMIN, ADMIN])}   # a real value beats backend/.env
    result = subprocess.run([sys.executable, "serve_app.py"], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "duplicate username 'admin'" in result.stderr
    assert "Traceback" not in result.stderr
