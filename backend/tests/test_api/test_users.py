"""
Tests for the admin-only user management API (app/api/users.py) and the
forced password change (app/api/deps.py's get_current_user).

Run from backend/:  python -m pytest tests/test_api/test_users.py -v
"""

import pytest


def create(client, username="tech9", role="user", password="Start#Pass9", confirm=None):
    return client.post("/users", json={"username": username, "role": role, "password": password,
                                       "confirm_password": password if confirm is None else confirm})


def login(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_the_admin_lists_accounts_without_system_users_or_hashes(admin_client):
    users = admin_client.get("/users").json()
    assert [u["username"] for u in users] == ["admin", "tech1"]
    assert set(users[0]) == {"username", "role", "disabled", "must_change_password", "created_at"}


def test_a_regular_user_cannot_manage_users(user_client):
    response = user_client.get("/users")
    assert response.status_code == 403 and response.json()["detail"] == "User management is available to admins only."


def test_a_new_user_must_change_the_password_before_anything_else(admin_client, second_client):
    assert create(admin_client).status_code == 201
    assert login(second_client, "tech9", "Start#Pass9").json()["must_change_password"] is True
    blocked = second_client.get("/incidents")
    assert blocked.status_code == 403 and "must change your password" in blocked.json()["detail"]
    assert second_client.get("/auth/me").status_code == 200              # allowed: the page needs it
    second_client.post("/auth/change-password", json={
        "current_password": "Start#Pass9", "new_password": "Own#Pass99", "confirm_password": "Own#Pass99"})
    assert second_client.get("/incidents").status_code == 200


@pytest.mark.parametrize("kwargs, status, reason", [
    ({"username": "ab"}, 400, "3-32 characters"),
    ({"username": "has space"}, 400, "3-32 characters"),
    ({"username": "AI-Assistant"}, 400, "reserved"),
    ({"username": "TECH1"}, 409, "already taken"),
    ({"role": "boss"}, 400, "role must be one of"),
    ({"password": "weakpass"}, 400, "uppercase letter"),
    ({"confirm": "Other#Pass9"}, 400, "do not match"),
])
def test_invalid_new_users_are_refused(admin_client, kwargs, status, reason):
    response = create(admin_client, **kwargs)
    assert response.status_code == status and reason in response.json()["detail"]


def test_disabling_a_user_ends_their_session_and_blocks_login(admin_client, user_client):
    assert admin_client.patch("/users/tech1", json={"disabled": True}).json()["disabled"] is True
    assert user_client.get("/auth/me").status_code == 401
    assert login(user_client, "tech1", "Demo#Pass2").status_code == 401
    admin_client.patch("/users/tech1", json={"disabled": False})
    assert login(user_client, "tech1", "Demo#Pass2").status_code == 200


def test_a_reset_password_ends_sessions_and_forces_a_change(admin_client, user_client):
    response = admin_client.post("/users/tech1/reset-password",
                                 json={"new_password": "Reset#Pass1", "confirm_password": "Reset#Pass1"})
    assert response.json()["must_change_password"] is True
    assert user_client.get("/auth/me").status_code == 401
    assert login(user_client, "tech1", "Reset#Pass1").json()["must_change_password"] is True


def test_role_change_takes_effect(admin_client, user_client):
    admin_client.patch("/users/tech1", json={"role": "admin"})
    assert user_client.get("/users").status_code == 200


@pytest.mark.parametrize("change", [{"role": "user"}, {"disabled": True}])
def test_an_admin_cannot_demote_or_disable_themselves(admin_client, change):
    response = admin_client.patch("/users/admin", json=change)
    assert response.status_code == 403 and "your own" in response.json()["detail"]


def test_one_admin_can_disable_another_which_logs_them_out(admin_client, second_client):
    create(admin_client, username="boss2", role="admin")
    login(second_client, "boss2", "Start#Pass9")
    second_client.post("/auth/change-password", json={
        "current_password": "Start#Pass9", "new_password": "Boss#Pass22", "confirm_password": "Boss#Pass22"})
    assert second_client.patch("/users/admin", json={"disabled": True}).status_code == 200   # two admins: allowed
    response = admin_client.patch("/users/boss2", json={"role": "user"})                    # admin is disabled now
    assert response.status_code == 401                                                     # ... and logged out


def test_unknown_or_system_users_are_404(admin_client):
    assert admin_client.patch("/users/nobody", json={"disabled": True}).status_code == 404
    assert admin_client.patch("/users/ai-assistant", json={"disabled": True}).status_code == 404
