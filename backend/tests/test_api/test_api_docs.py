"""
Tests for the API documentation behind API_DOCS_ACCESS (app/api/docs.py):
by default only for someone logged in; "public" opens it to anyone; a bad
value is refused on the Server settings page.

Run from backend/:  python -m pytest tests/test_api/test_api_docs.py -v
"""

import pytest
from fastapi.testclient import TestClient

DOC_PAGES = ("/docs", "/redoc")


def test_by_default_strangers_are_sent_to_the_login_page(app_client):
    for page in DOC_PAGES:
        response = app_client.get(page, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
    assert app_client.get("/openapi.json").status_code == 401


def test_a_logged_in_user_reads_them(user_client):
    for page in DOC_PAGES:
        assert user_client.get(page).status_code == 200
    schema = user_client.get("/openapi.json").json()
    assert "/incidents" in schema["paths"] and "/openapi.json" not in schema["paths"]


def test_public_opens_them_to_anyone(app_client, monkeypatch):
    monkeypatch.setenv("API_DOCS_ACCESS", "public")
    stranger = TestClient(app_client.app)
    assert stranger.get("/docs").status_code == 200 and stranger.get("/openapi.json").status_code == 200


def test_a_bad_value_is_refused_on_the_page(admin_client):
    response = admin_client.put("/server/config", json={"values": {"API_DOCS_ACCESS": "everyone"}})
    assert response.status_code == 400 and "login or public" in response.json()["detail"]
    saved = admin_client.put("/server/config", json={"values": {"API_DOCS_ACCESS": "public"}}).json()
    assert saved["changed"] == ["API_DOCS_ACCESS"]
