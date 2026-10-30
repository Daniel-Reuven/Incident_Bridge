"""
Finishing the first-start setup in tests (app/api/setup.py), exactly as the
setup page does: the setup code from the running app, /setup/start (the
recovery key), /setup/complete with the demo admin. The regular user tech1
comes from INCIDENT_BRIDGE_USERS, set in tests/conftest.py.

    with TestClient(app) as client:
        finish_setup(client)
"""

DEMO_ADMIN = ("admin", "Demo#Pass1")


def finish_setup(client, admin=DEMO_ADMIN, ai=None) -> str:
    """Complete the setup of the app behind `client`; returns the recovery key."""
    session = client.app.state.setup
    assert session is not None, "the app is already set up"
    started = client.post("/setup/start", json={"code": session.code})
    assert started.status_code == 200, started.text
    recovery = started.json()["recovery_key"]
    body = {"code": session.code, "admin_username": admin[0], "admin_password": admin[1],
            "confirm_password": admin[1], "ai": ai,
            "recovery_check": "-".join(recovery.split("-")[-2:]) if recovery else None}
    done = client.post("/setup/complete", json=body)
    assert done.status_code == 200, done.text
    return recovery


def add_demo_users(state):
    """
    The demo admin and tech1 in an AppState built directly with
    AppState.create() (no app, so no setup page) - what the setup page would
    have created. Saved to the database, so a restart finds them.
    """
    from app.models import Role, User
    for username, role, password in (("admin", Role.ADMIN, DEMO_ADMIN[1]), ("tech1", Role.USER, "Demo#Pass2")):
        if state.users.get(username) is None:
            state.users.add(User(username, role, password))
    return state
