"""In-memory stores: pre-provisioned users, and every incident ever created."""

import json
import os
from typing import Dict, Iterable, Iterator, Optional

from app.models import Incident, Role, User
from app.persistence import SqliteIncidentStore


class UserStore:
    """Simple in-memory username -> User lookup. No self-registration - see seed_users_from_env below."""

    def __init__(self):
        self._users: Dict[str, User] = {}

    def add(self, user: User) -> None:
        self._users[user.username] = user

    def get(self, username: str) -> Optional[User]:
        return self._users.get(username)

    def all_users(self) -> Iterator[User]:
        yield from self._users.values()


_DEFAULT_USERS_WARNING = (
    "INCIDENT_BRIDGE_USERS is not set - falling back to a demo admin/user pair "
    "with well-known passwords (admin/Passw0rd1, tech1/Passw0rd2). Do NOT use "
    "this outside local development; set INCIDENT_BRIDGE_USERS before deploying "
    "anywhere reachable by anyone else."
)


def seed_users_from_env(env_var: str = "INCIDENT_BRIDGE_USERS") -> UserStore:
    """
    Build the UserStore from an environment variable holding a JSON array,
    e.g. (see .env.example):

        INCIDENT_BRIDGE_USERS=[{"username":"admin","password":"Passw0rd1","role":"admin"},
                                {"username":"tech1","password":"Passw0rd2","role":"user"}]

    This is the "pre-provisioned via env, no self-registration" mechanism
    from the design doc (README section 6). Falls back to an insecure demo
    pair (with a loud warning) only so the app can start with zero setup
    during local development.
    """
    store = UserStore()
    raw = os.environ.get(env_var)
    if not raw:
        print(f"WARNING: {_DEFAULT_USERS_WARNING}")
        store.add(User(username="admin", role=Role.ADMIN, password="Passw0rd1"))
        store.add(User(username="tech1", role=Role.USER, password="Passw0rd2"))
        return store

    for entry in json.loads(raw):
        store.add(User(username=entry["username"], role=Role(entry["role"]), password=entry["password"]))
    return store


class IncidentRepository:
    """
    Holds every incident created so far, by id, regardless of which queue
    (or no queue, once closed) it currently belongs to. The queues hold
    references to these same objects and only govern ordering/availability
    for work - this repository is what the API uses for lookups, listing,
    and comments, so an incident is never "lost" just because it left a
    queue (e.g. a claimed fault, or a completed maintenance task).

    Optionally backed by a SqliteIncidentStore for persistence across
    restarts (see app/persistence.py and app/state.py) - users chose
    SQLite-for-incidents-only for this step, so `store` can be None for a
    memory-only repository (e.g. in a future unit test that shouldn't
    touch disk at all).
    """

    def __init__(self, store: Optional[SqliteIncidentStore] = None):
        self._incidents: Dict[str, Incident] = {}
        self._store = store

    def add(self, incident: Incident) -> None:
        """Register a newly-created incident. Equivalent to save() - see below."""
        self.save(incident)

    def save(self, incident: Incident) -> None:
        """
        Call this after creating an incident, AND after any later mutation
        (status change, closing, a new comment, a severity change) - every
        mutating endpoint in app/api/incidents.py calls this exactly once,
        right after the domain-level mutation succeeds. Keeps the
        in-memory copy current and, if a store is configured, persists it.
        """
        self._incidents[incident.id] = incident
        if self._store:
            self._store.save_incident(incident)

    def bulk_load(self, incidents: Iterable[Incident]) -> None:
        """
        Populate the in-memory dict from already-persisted incidents at
        startup (see app/state.py) - does NOT re-persist them, since
        they're already in the store this data came from.
        """
        for incident in incidents:
            self._incidents[incident.id] = incident

    def get(self, incident_id: str) -> Incident:
        """Raises KeyError (mapped to HTTP 404 - see app/api/app.py) if not found."""
        try:
            return self._incidents[incident_id]
        except KeyError:
            raise KeyError(f"Incident {incident_id} not found.") from None

    def list_all(self) -> Iterator[Incident]:
        yield from self._incidents.values()
