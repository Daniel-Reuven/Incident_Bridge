"""
Shared pytest fixtures for the entire Incident Bridge backend test suite.

pytest automatically discovers this file (it MUST be named exactly
`conftest.py`) and makes every fixture defined here available to every
test file in this directory AND all subdirectories (tests/test_models/,
tests/test_services/, tests/test_queues/, tests/test_api/, ...) with NO
import needed - just add the fixture's name as a parameter to a test
function and pytest supplies it automatically.

Docs: https://docs.pytest.org/en/stable/reference/fixtures.html

WHAT'S IN HERE
--------------
Domain-layer fixtures (plain Python objects, no HTTP/database at all) -
used by tests/test_models/*.py, tests/test_queues/*.py, and test_context.py:
    - admin_user, regular_user: ready-to-use User objects.

HTTP/API fixtures (spin up the real FastAPI app) - used by tests/test_api/*.py:
    - app_client:    one fresh, fully isolated app instance per test
                      (in-memory SQLite database, empty queues), NOT logged in.
    - second_client: a second, independent login session against the SAME
                      running app instance as app_client (see its
                      docstring below for exactly why this is safe).
    - admin_client:  app_client, already logged in as the demo admin.
    - user_client:   second_client, already logged in as the demo user.

WHY EVERY TEST GETS ITS OWN APP INSTANCE
-----------------------------------------
`app_client` is function-scoped (pytest's default), meaning a brand new
TestClient - and therefore a brand new AppState, a brand new in-memory
database, and empty maintenance/fault queues - is built for every single
test function. This is a little slower than sharing one app across a
whole test file, but it removes an entire category of bugs where test B
only fails because test A ran first and left something behind in a shared
queue. Worth the trade-off at this project's size.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.models import Role, User


# --------------------------------------------------------------------
# Domain-layer fixtures - plain Python objects, no HTTP/DB involved.
# Used by tests/test_models/*.py, tests/test_queues/*.py, test_context.py.
# --------------------------------------------------------------------

@pytest.fixture
def admin_user() -> User:
    """A fresh admin User, valid under PasswordPolicy (8+ chars, alphanumeric only)."""
    return User(username="test-admin", role=Role.ADMIN, password="AdminPass1")


@pytest.fixture
def regular_user() -> User:
    """A fresh non-admin User, valid under PasswordPolicy."""
    return User(username="test-user", role=Role.USER, password="UserPass1")


# --------------------------------------------------------------------
# API-layer fixtures - a real running FastAPI app, talked to over HTTP
# via FastAPI's TestClient (which wraps httpx under the hood).
# Used by tests/test_api/*.py.
# --------------------------------------------------------------------

@pytest.fixture
def app_client():
    """
    A fresh FastAPI TestClient for exactly one test function, backed by a
    throwaway in-memory SQLite database - so every test starts from a
    completely empty, isolated state (no leftover incidents, no shared
    file on disk, no interference between tests running in any order).
    This mirrors the ":memory:" trick already used in
    backend/smoke_test_api.py.

    `with TestClient(app) as client:` is what actually triggers FastAPI's
    startup/shutdown lifespan (see app/api/app.py's `lifespan()`), which
    is what builds AppState (reading DATABASE_PATH at that moment) and
    stores it on `app.state.incident_bridge`. Setting the env var BEFORE
    entering that `with` block is what makes AppState.create() see the
    in-memory database for this test, instead of the real
    incident_bridge.db file on disk.
    """
    os.environ["DATABASE_PATH"] = ":memory:"
    from app.api.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def second_client(app_client):
    """
    A second TestClient pointed at the exact SAME `app` object as
    app_client - and therefore the same already-initialized
    `app.state.incident_bridge` - but with its own independent cookie
    jar, so it can log in as a different user without disturbing
    app_client's session. Use this (via the user_client fixture below)
    together with admin_client to simulate two different people using
    the app at the same time.

    This deliberately does NOT do `with TestClient(app) as ...:` again -
    doing so would re-run the startup lifespan a second time and replace
    app.state.incident_bridge with a brand new, empty AppState, silently
    disconnecting this client from whatever app_client already did (e.g.
    an incident app_client just created would appear to vanish). Because
    `app_client` (the fixture this one depends on) already ran the
    lifespan once, `app.state` is already populated - this second
    TestClient just needs a plain reference to that same `app` object to
    start making requests against that same shared state.
    """
    from app.api.app import app

    return TestClient(app)


@pytest.fixture
def admin_client(app_client):
    """app_client, already logged in as the demo admin (admin / Passw0rd1)."""
    response = app_client.post("/auth/login", json={"username": "admin", "password": "Passw0rd1"})
    assert response.status_code == 200, f"fixture setup failed: admin login returned {response.status_code}"
    return app_client


@pytest.fixture
def user_client(second_client):
    """
    An independent session (its own cookies) logged in as the demo
    regular user (tech1 / Passw0rd2), sharing app_client's backend state.
    Request this together with admin_client in the same test to simulate
    two different people (an admin and a regular user) working the same
    incidents at the same time.
    """
    response = second_client.post("/auth/login", json={"username": "tech1", "password": "Passw0rd2"})
    assert response.status_code == 200, f"fixture setup failed: user login returned {response.status_code}"
    return second_client
