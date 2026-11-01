"""
Tests for display names: app/display_names.py (the template, tidying empty
fields, the lighter location part) and the API around them
(app/api/users.py: directory, details, the default format, the preview).

Run from backend/:  python -m pytest tests/test_display_names.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.display_names import PRESETS, render, validate_format
from tests.install_helper import finish_setup

DANA = {"first_name": "Dana", "last_name": "Levi", "job_title": "Technician", "location": "Haifa"}


@pytest.mark.parametrize("template, fields, text, secondary", [
    ("{first-name} {last-name} - {job-title} ({location})", DANA, "Dana Levi - Technician (Haifa)", "(Haifa)"),
    ("{first-name} {last-name} - {job-title}({location})", DANA, "Dana Levi - Technician(Haifa)", "(Haifa)"),
    ("{first-name} {last-name} - {location}", DANA, "Dana Levi - Haifa", "Haifa"),
    ("{last-name}, {first-name}", DANA, "Levi, Dana", None),
    ("{first-name} {middle-name} {last-name}", DANA, "Dana Levi", None),                 # no double space
    ("{first-name} {last-name} - {job-title} ({location})", {"first_name": "Dana"}, "Dana", None),
    ("{first-name} - {job-title} ({location})", {"first_name": "Dana", "location": "Haifa"}, "Dana (Haifa)", "(Haifa)"),
    ("{job-title} - {first-name}", {"first_name": "Dana"}, "Dana", None),                # no leading " - "
    ("{first-name} {last-name}", {}, "tech1", None),                                     # nothing left: the username
    ("{username} ({first-name})", {}, "tech1", None),
])
def test_render_fills_the_template_and_tidies_what_empty_fields_leave(template, fields, text, secondary):
    rendered, parts = render(template, fields, "tech1")
    assert rendered == text and "".join(part for part, _ in parts) == text
    assert [part for part, is_secondary in parts if is_secondary] == ([secondary] if secondary else [])


@pytest.mark.parametrize("template, message", [
    ("", "empty"), ("{first-name} {nickname}", "Unknown placeholder"), ("Mr. Smith", "at least one placeholder"),
    ("{first-name}" + "x" * 120, "too long"),
])
def test_invalid_formats_are_refused(template, message):
    with pytest.raises(ValueError, match=message):
        validate_format(template)


def test_every_preset_is_valid():
    assert all(validate_format(preset) == preset for preset in PRESETS)


# --- the API ---------------------------------------------------------------------------------

def test_the_directory_shows_everyone_by_display_name(admin_client, user_client):
    admin_client.patch("/users/tech1/details", json=DANA)
    people = user_client.get("/users/directory").json()["people"]          # any logged-in user
    assert people["tech1"]["display_name"] == "Dana Levi"                   # the default format
    assert people["admin"]["display_name"] == "admin"                       # no details: the username
    assert people["ai-assistant"]["display_name"] == "AI assistant"
    assert user_client.get("/auth/me").json()["display_name"] == "Dana Levi"


def test_the_default_format_and_an_own_format(admin_client, user_client):
    admin_client.patch("/users/tech1/details", json=DANA)
    admin_client.patch("/users/admin/details", json={"first_name": "Avi", "job_title": "Team lead"})
    assert admin_client.put("/users/display-format",
                            json={"format": "{first-name} {last-name} - {job-title} ({location})"}).status_code == 200
    people = user_client.get("/users/directory").json()["people"]
    assert people["tech1"]["display_parts"] == [["Dana Levi - Technician ", False], ["(Haifa)", True]]
    assert people["admin"]["display_name"] == "Avi - Team lead"
    admin_client.patch("/users/admin/details", json={"display_format": "{username}"})     # an own format
    assert user_client.get("/users/directory").json()["people"]["admin"]["display_name"] == "admin"
    admin_client.patch("/users/admin/details", json={"display_format": ""})               # back to the default
    assert user_client.get("/users/directory").json()["people"]["admin"]["display_name"] == "Avi - Team lead"


def test_only_admins_change_details_and_formats(user_client):
    assert user_client.patch("/users/tech1/details", json={"first_name": "Me"}).status_code == 403
    assert user_client.put("/users/display-format", json={"format": "{username}"}).status_code == 403
    assert user_client.post("/users/display-preview", json={"format": "{username}"}).status_code == 403


@pytest.mark.parametrize("body, reason", [
    ({"first_name": "x" * 61}, "too long"), ({"display_format": "{nickname}"}, "Unknown placeholder"),
])
def test_invalid_details_are_refused(admin_client, body, reason):
    response = admin_client.patch("/users/tech1/details", json=body)
    assert response.status_code == 400 and reason in response.json()["detail"]


def test_the_preview_saves_nothing(admin_client):
    shown = admin_client.post("/users/display-preview", json={"format": "{last-name}, {first-name}", **DANA,
                                                              "username": "x"}).json()
    assert shown["display_name"] == "Levi, Dana"
    assert admin_client.get("/users/display-format").json()["format"] == "{first-name} {last-name}"


def test_new_users_can_have_names_and_the_log_names_the_fields_not_the_values(admin_client, caplog):
    created = admin_client.post("/users", json={"username": "nora", "role": "user", "password": "Aboard-Today#7",
                                                "confirm_password": "Aboard-Today#7", "first_name": "Nora",
                                                "last_name": "Cohen"}).json()
    assert created["display_name"] == "Nora Cohen"
    caplog.set_level("INFO", logger="incident_bridge.audit")
    admin_client.patch("/users/nora/details", json={"job_title": "Technician"})
    record = next(r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "users.details_changed")
    assert record["fields"] == ["job_title"] and "Technician" not in json.dumps(record)


def test_names_survive_a_restart_and_come_from_the_setup_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("INCIDENT_BRIDGE_USERS", json.dumps([{"username": "tech1", "password": "Demo#Pass2",
                                                             "role": "user", "first_name": "Dana",
                                                             "location": "Haifa"}]))
    from app.api.app import app
    with TestClient(app) as client:
        session = app.state.setup
        recovery = client.post("/setup/start", json={"code": session.code}).json()["recovery_key"]
        client.post("/setup/complete", json={"code": session.code, "admin_username": "admin",
                                              "admin_password": "Demo#Pass1", "confirm_password": "Demo#Pass1",
                                              "admin_first_name": "Avi", "admin_last_name": "Ben",
                                              "recovery_check": "-".join(recovery.split("-")[-2:])})
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        client.put("/users/display-format", json={"format": "{first-name} {last-name} ({location})"})
    with TestClient(app) as client:                                         # a restart
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        people = client.get("/users/directory").json()["people"]
        assert people["admin"]["display_name"] == "Avi Ben"
        assert people["tech1"]["display_parts"] == [["Dana ", False], ["(Haifa)", True]]
        assert client.get("/logs/verify").json()["ok"] is True
