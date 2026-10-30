"""
Tests for the requirements demo page: app/requirements_demo.py (every demo
runs and shows something, every piece of code it points to exists, every
page it links to exists) and its API (app/api/demo.py: logged-in users only,
unknown ids, and the app's own data untouched by a run).

Run from backend/:  python -m pytest tests/test_requirements_demo.py -v
"""

from pathlib import Path

import pytest

from app import requirements_demo
from app.requirements_demo import REQUIREMENTS

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@pytest.mark.parametrize("requirement", REQUIREMENTS, ids=[r.id for r in REQUIREMENTS])
def test_every_demo_runs_and_shows_something(requirement):
    result = requirements_demo.run(requirement.id)
    assert result["error"] is None and result["output"]


def test_one_entry_per_stage_1_requirement_with_unique_ids():
    assert len(REQUIREMENTS) == 16                      # README section 16, one row each
    assert len({r.id for r in REQUIREMENTS}) == len(REQUIREMENTS)


@pytest.mark.parametrize("requirement", REQUIREMENTS, ids=[r.id for r in REQUIREMENTS])
def test_the_code_shown_is_the_app_s_own_and_the_links_exist(requirement):
    shown = requirements_demo.describe(requirement)
    assert shown["brief"] and shown["uses"] and shown["demo"]["source"].startswith("def demo_")
    for code in shown["code"]:
        assert code["file"].startswith("backend/app/") and code["line"] > 0 and code["source"].strip()
        name = code["ref"].split(".")[-1]
        assert f"def {name}" in code["source"] or f"class {name}" in code["source"]
    for use in shown["uses"]:
        if use["link"]:
            assert (FRONTEND / use["link"].lstrip("/")).is_file(), use["link"]


def test_a_demo_that_breaks_reports_why(monkeypatch):
    def broken(out):
        out("before")
        raise RuntimeError("on purpose")
    monkeypatch.setattr(requirements_demo, "_BY_ID", {"x": requirements_demo.Requirement(
        "x", "t", "b", broken, (), ())})
    result = requirements_demo.run("x")
    assert result["output"] == ["before"] and result["error"] == "RuntimeError: on purpose"


# --- the API ----------------------------------------------------------------------------------

def test_only_for_logged_in_users(app_client):
    assert app_client.get("/demo/requirements").status_code == 401
    assert app_client.post("/demo/requirements/deque/run").status_code == 401


def test_the_list_and_a_run(user_client):
    listed = user_client.get("/demo/requirements").json()
    assert listed["stage"] == 1 and [r["id"] for r in listed["requirements"]] == [r.id for r in REQUIREMENTS]
    ran = user_client.post("/demo/requirements/deque/run").json()
    assert any("IndexError" in line for line in ran["output"]) and ran["error"] is None
    assert user_client.post("/demo/requirements/no-such/run").status_code == 404


def test_a_run_never_touches_the_app_s_data(admin_client):
    before = {i["id"] for i in admin_client.get("/incidents").json()}
    users_before = {u["username"] for u in admin_client.get("/users").json()}
    for requirement in REQUIREMENTS:
        assert admin_client.post(f"/demo/requirements/{requirement.id}/run").json()["error"] is None
    assert {i["id"] for i in admin_client.get("/incidents").json()} == before
    assert {u["username"] for u in admin_client.get("/users").json()} == users_before
