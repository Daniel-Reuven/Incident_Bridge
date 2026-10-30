"""
API test: the built-in "ai-assistant" system user cannot log in
(POST /auth/login), whatever password is tried.

Run from backend/:  python -m pytest tests/test_api/test_system_user_login.py -v
"""

import pytest


@pytest.mark.parametrize("password", ["", "ai-assistant", "Passw0rd!1"])
def test_the_ai_user_cannot_log_in(app_client, password):
    response = app_client.post("/auth/login", json={"username": "ai-assistant", "password": password})
    assert response.status_code == 401
