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

import pytest


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
    response = app_client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_user_login_succeeds_and_reports_user_role(app_client):
    response = app_client.post("/auth/login", json={"username": "tech1", "password": "Demo#Pass2"})
    assert response.status_code == 200
    assert response.json()["role"] == "user"


def test_me_reflects_the_logged_in_user(admin_client):
    response = admin_client.get("/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


def change(client, current="Demo#Pass1", new="NewValid#1", confirm=None):
    return client.post("/auth/change-password", json={
        "current_password": current, "new_password": new, "confirm_password": new if confirm is None else confirm})


def test_change_password_with_the_wrong_current_password_is_rejected(admin_client):
    assert change(admin_client, current="wrong").status_code == 403


@pytest.mark.parametrize("new, reason", [
    ("bad", "at least 8 characters"),
    ("alllowercase1!", "uppercase letter"),
    ("NoSymbol123", "symbol"),
    ("Xadmin#2026", "must not contain the username"),
    ("Demo#Pass1", "different from the current one"),
])
def test_change_password_with_a_policy_violating_new_password_is_rejected(admin_client, new, reason):
    response = change(admin_client, new=new)
    assert response.status_code == 400 and reason in response.json()["detail"]


def test_change_password_needs_a_matching_confirmation(admin_client):
    response = change(admin_client, new="NewValid#1", confirm="NewValid#2")
    assert response.status_code == 400 and "do not match" in response.json()["detail"]


def test_change_password_success_swaps_old_for_new_and_keeps_this_session(admin_client):
    assert change(admin_client).status_code == 200
    assert admin_client.get("/auth/me").status_code == 200          # this session was renewed

    # The old password must stop working, and the new one must start working.
    admin_client.post("/auth/logout")
    stale = admin_client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    assert stale.status_code == 401
    fresh = admin_client.post("/auth/login", json={"username": "admin", "password": "NewValid#1"})
    assert fresh.status_code == 200


def test_changing_the_password_ends_the_users_other_sessions(admin_client, second_client):
    assert second_client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"}).status_code == 200
    assert change(admin_client).status_code == 200
    assert second_client.get("/auth/me").status_code == 401
    assert admin_client.get("/auth/me").status_code == 200


def test_the_password_policy_is_public(app_client):
    body = app_client.get("/auth/password-policy").json()
    assert body["min_length"] == 8 and body["guide_url"] == "/guide.html#password-policy"
    assert [r["id"] for r in body["rules"]][:5] == ["length", "upper", "lower", "digit", "symbol"]


def test_repeated_failed_logins_lock_the_username(app_client):
    for _ in range(5):
        assert app_client.post("/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    locked = app_client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    # another username is not locked by admin's failures
    assert app_client.post("/auth/login", json={"username": "tech1", "password": "Demo#Pass2"}).status_code == 200


def test_unknown_and_disabled_users_get_the_same_answer_as_a_wrong_password(admin_client, second_client):
    admin_client.patch("/users/tech1", json={"disabled": True})
    for username, password in (("nobody", "Demo#Pass2"), ("tech1", "Demo#Pass2"), ("admin", "wrong")):
        response = second_client.post("/auth/login", json={"username": username, "password": password})
        assert (response.status_code, response.json()) == (401, {"detail": "Invalid username or password."})


def test_every_response_carries_the_security_headers(app_client):
    headers = app_client.get("/health").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in app_client.get("/docs").headers      # Swagger UI needs its CDN


@pytest.mark.parametrize("path", ["/dashboard.html", "/js/api.js", "/css/styles.css", "/health"])
def test_interface_files_are_always_revalidated(app_client, path):
    """No stale JavaScript/CSS after an upgrade: the browser must re-check every file with the server."""
    response = app_client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"


def test_an_unchanged_file_is_answered_with_304(app_client):
    first = app_client.get("/js/api.js")
    again = app_client.get("/js/api.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_logout_actually_clears_the_session(admin_client):
    response = admin_client.post("/auth/logout")
    assert response.status_code == 200
    after_logout = admin_client.get("/auth/me")
    assert after_logout.status_code == 401
