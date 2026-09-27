"""
Integration tests for the /auth/* endpoints (app/api/auth.py), exercised
over real HTTP via FastAPI's TestClient - see tests/conftest.py for the
app_client / admin_client / user_client fixtures used below (pytest
supplies them automatically; no import is needed in this file).

Unlike tests/test_models and tests/test_queues (pure unit tests, no HTTP
or database involved), these are "integration" tests: they go through
the whole stack - session middleware, routing, the domain layer, and the
(isolated, in-memory) SQLite persistence layer - the same way a real
browser talking to a real server would.
"""


def test_health_check_does_not_require_login(app_client):
    response = app_client.get("/health")
    assert response.status_code == 200


def test_unauthenticated_me_is_rejected(app_client):
    response = app_client.get("/auth/me")
    assert response.status_code == 401


def test_login_with_wrong_password_is_rejected(app_client):
    response = app_client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401


def test_login_with_unknown_username_is_rejected(app_client):
    response = app_client.post("/auth/login", json={"username": "nobody", "password": "whatever"})
    assert response.status_code == 401


def test_admin_login_succeeds_and_reports_admin_role(app_client):
    response = app_client.post("/auth/login", json={"username": "admin", "password": "Passw0rd1"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_user_login_succeeds_and_reports_user_role(app_client):
    response = app_client.post("/auth/login", json={"username": "tech1", "password": "Passw0rd2"})
    assert response.status_code == 200
    assert response.json()["role"] == "user"


def test_me_reflects_the_logged_in_user(admin_client):
    response = admin_client.get("/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


def test_change_password_with_the_wrong_current_password_is_rejected(admin_client):
    response = admin_client.post(
        "/auth/change-password", json={"current_password": "wrong", "new_password": "NewValid1"}
    )
    assert response.status_code == 403


def test_change_password_with_a_policy_violating_new_password_is_rejected(admin_client):
    response = admin_client.post(
        "/auth/change-password", json={"current_password": "Passw0rd1", "new_password": "bad"}
    )
    assert response.status_code == 400


def test_change_password_success_swaps_old_for_new(admin_client):
    response = admin_client.post(
        "/auth/change-password", json={"current_password": "Passw0rd1", "new_password": "NewValid1"}
    )
    assert response.status_code == 200

    # The old password must stop working, and the new one must start working.
    admin_client.post("/auth/logout")
    stale = admin_client.post("/auth/login", json={"username": "admin", "password": "Passw0rd1"})
    assert stale.status_code == 401
    fresh = admin_client.post("/auth/login", json={"username": "admin", "password": "NewValid1"})
    assert fresh.status_code == 200


def test_logout_actually_clears_the_session(admin_client):
    response = admin_client.post("/auth/logout")
    assert response.status_code == 200
    after_logout = admin_client.get("/auth/me")
    assert after_logout.status_code == 401
