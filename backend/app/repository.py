"""In-memory stores: pre-provisioned users, and every incident ever created."""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional

from app.models import Fault, Incident, MaintenanceTask, Role, User
from app.persistence import SqliteIncidentStore


class UserProvisioningError(Exception):
    """
    INCIDENT_BRIDGE_USERS is unusable (invalid JSON, an invalid entry, or a
    duplicate username). Raised by seed_users_from_env() so the app refuses
    to start instead of running with a half-configured user list; the
    message lists every problem found and says how to fix it.
    serve_app.py catches it before the server starts and exits with that
    message alone (no traceback).
    """


class UserStore:
    """
    Simple in-memory username -> User lookup. No self-registration - see
    seed_users_from_env below.

    Explicit duplicate decision: usernames are unique, compared
    case-insensitively ("Admin" and "admin" would be two near-identical
    logins), and add() raises ValueError for a duplicate instead of silently
    replacing the existing user. Lookups with get() stay exact-case, so the
    login name is the username exactly as provisioned.
    """

    def __init__(self):
        self._users: Dict[str, User] = {}

    def __str__(self) -> str:
        return f"<UserStore: {len(self._users)} users>"

    def __repr__(self) -> str:
        """Developer view: the provisioned usernames (never passwords or hashes)."""
        return f"UserStore(usernames={sorted(self._users)!r})"

    def add(self, user: User) -> None:
        """Add a user. Raises ValueError if the username is already taken (case-insensitive)."""
        existing = self.find_case_insensitive(user.username)
        if existing is not None:
            raise ValueError(f"username {user.username!r} is already used by {existing.username!r}")
        self._users[user.username] = user

    def find_case_insensitive(self, username: str) -> Optional[User]:
        """The user whose username equals `username` ignoring letter case, or None."""
        wanted = username.lower()
        return next((u for u in self._users.values() if u.username.lower() == wanted), None)

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


def seed_users_from_env(env_var: str = "INCIDENT_BRIDGE_USERS", warn: bool = True) -> UserStore:
    """
    Build the UserStore from an environment variable holding a JSON array,
    e.g. (see .env.example):

        INCIDENT_BRIDGE_USERS=[{"username":"admin","password":"Passw0rd1","role":"admin"},
                                {"username":"tech1","password":"Passw0rd2","role":"user"}]

    This is the "pre-provisioned via env, no self-registration" mechanism
    from the design doc (README section 6). Falls back to an insecure demo
    pair (with a loud warning, unless warn=False) only so the app can start
    with zero setup during local development.

    Fail-fast: when the variable IS set, every entry is checked and ALL
    problems are collected - invalid JSON, not a list, an empty list, an
    entry that User.from_dict() rejects (missing field, bad role, weak
    password), or a duplicate username (case-insensitive, naming the entry
    it repeats). If there is any problem, UserProvisioningError is raised
    with one message listing them all, so the app never starts with a
    partly-loaded or ambiguous user list.

    `warn` exists so serve_app.py can validate the variable before starting
    the server without printing the demo-users warning twice.
    """
    store = UserStore()
    raw = os.environ.get(env_var)
    if not raw or not raw.strip():
        if warn:
            print(f"WARNING: {_DEFAULT_USERS_WARNING}")
        store.add(User(username="admin", role=Role.ADMIN, password="Passw0rd1"))
        store.add(User(username="tech1", role=Role.USER, password="Passw0rd2"))
        return store

    def fail(problems: List[str]) -> UserProvisioningError:
        listed = "\n".join(f"  - {p}" for p in problems)
        return UserProvisioningError(
            f"{env_var} has {len(problems)} problem(s) - the app will not start until they are fixed:\n"
            f"{listed}\nFix the value in backend/.env (or your environment) and start again."
        )

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise fail([f"not valid JSON ({exc.msg} at position {exc.pos})"]) from None
    if not isinstance(entries, list):
        raise fail([f"must be a JSON array of users, got {type(entries).__name__}"])
    if not entries:
        raise fail(["the array is empty - provision at least one user"])

    problems: List[str] = []
    first_entry_of: Dict[str, int] = {}          # lower-cased username -> entry number that defined it
    for number, entry in enumerate(entries, start=1):
        try:
            user = User.from_dict(entry)
        except ValueError as exc:
            problems.append(f"entry {number}: {exc}")
            continue
        key = user.username.lower()
        if key in first_entry_of:
            problems.append(f"entry {number}: duplicate username {user.username!r} "
                            f"(already defined in entry {first_entry_of[key]})")
            continue
        first_entry_of[key] = number
        store.add(user)
    if problems:
        raise fail(problems)
    return store


@dataclass
class SeedLoadResult:
    """
    Outcome of one IncidentRepository.load_from_jsonl[_lines]() call.
    created_ids (not just a count) is what lets a caller that needs to do
    more than just report a summary - see app/api/incidents.py's
    import_jsonl(), which enqueues each new incident into the live
    maintenance/fault queue and broadcasts it - look each one up via
    IncidentRepository.get() afterwards. `created` stays available as a
    computed property (not a stored field) purely so existing "how many
    got created" call sites don't need to change to len(created_ids).
    """
    created_ids: List[str] = field(default_factory=list)
    skipped_duplicate_ids: List[str] = field(default_factory=list)
    skipped_invalid: List[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return len(self.created_ids)

    def __str__(self) -> str:
        return (f"SeedLoadResult(created={self.created}, "
                f"skipped_duplicates={len(self.skipped_duplicate_ids)}, "
                f"skipped_invalid={len(self.skipped_invalid)})")


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

    def __str__(self) -> str:
        return f"<IncidentRepository: {len(self._incidents)} incidents loaded>"

    def __repr__(self) -> str:
        """Developer view: how many incidents are held and which store (if any) persists them."""
        return f"IncidentRepository(incidents={len(self._incidents)}, store={self._store!r})"

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

    def save_all(self, incidents: Iterable[Incident]) -> None:
        """
        save() every incident in `incidents`, one after another. Used after a
        queue change (app/api/incidents.py's _persist_maintenance_queue):
        closing or completing one maintenance task shifts the queue_position
        of every task behind it, and each of those has to be persisted too -
        otherwise the stored positions go stale, and the order rebuilt at the
        next startup (app/state.py) would be wrong. Not a single database
        transaction (each save commits on its own), which is fine at this
        project's scale.
        """
        for incident in incidents:
            self.save(incident)

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

    def load_from_jsonl(self, path: str, users: "UserStore") -> SeedLoadResult:
        """
        Reads a seed-data FILE of synthetic incidents - one JSON object per
        line, e.g. data/sample_data.jsonl - from disk. Thin wrapper around
        load_from_jsonl_lines() (below), which is where the actual
        line-by-line parsing/validation/dedup logic lives: this method's
        only job is opening the file and handing its lines over, one at a
        time, without ever calling read()/readlines() (so the file is
        never held in memory all at once, however large it might be).

        Used by backend/seed_from_jsonl.py to read a local file's content
        before sending it to a running server (see load_from_jsonl_lines'
        docstring for why that content, not this path, is what actually
        gets processed). Raises only for problems with the file itself
        (e.g. it doesn't exist) - see load_from_jsonl_lines for how
        problems with individual records inside it are handled.
        """
        with open(path, encoding="utf-8") as f:
            return self.load_from_jsonl_lines(f, users)

    def load_from_jsonl_lines(self, lines: Iterable[str], users: "UserStore") -> SeedLoadResult:
        """
        The actual seed-loading logic, over any iterable of JSONL text
        lines - an open file (see load_from_jsonl above), or
        `some_string.splitlines()` for content that arrived some other
        way (e.g. app/api/incidents.py's import_jsonl endpoint, which
        receives raw text in an HTTP request body rather than a path on
        this process's filesystem - the server may not even be running on
        the same machine as whoever is importing). Saves each valid,
        non-duplicate record exactly as if it had been created through the
        API (same save(), same persistence if a store is configured) -
        but does NOT enqueue it into a live MaintenanceQueue/
        FaultPriorityQueue or broadcast a live-update event; that's an
        application-layer concern for whichever caller has access to
        those (see import_jsonl for the one that does).

        Never called automatically at normal app startup - this is always
        an explicit, repeatable seeding action.

        Explicit duplicate-id decision: a record whose 'id' already exists
        in this repository is SKIPPED, not overwritten and not an error -
        this is what makes re-running an import against the same data
        idempotent (running it twice adds nothing the second time).

        Explicit invalid-record decision: a record with an unknown 'kind',
        a missing/invalid 'id', a missing/blank/over-long title or
        description, invalid fault 'details' (wrong types, unknown keys -
        see SeverityScorer.validate_details), an unresolvable created_by /
        assigned_to username, a line that is not a JSON object, or invalid
        JSON on that line is SKIPPED and recorded in the result with a
        reason - it does not raise and does not stop the rest of the data
        from loading. Every one of those problems surfaces as a ValueError
        (json.JSONDecodeError is a ValueError subclass), which is the only
        exception caught below - any other exception would be a real bug
        and is deliberately not hidden.
        """
        result = SeedLoadResult()
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue  # blank lines are not errors, just skipped silently
            try:
                incident = self._incident_from_jsonl_line(line, users)
            except (ValueError, json.JSONDecodeError) as exc:
                result.skipped_invalid.append(f"line {line_number}: {exc}")
                continue
            if incident.id in self._incidents:
                result.skipped_duplicate_ids.append(incident.id)
                continue
            self.save(incident)
            result.created_ids.append(incident.id)
        return result

    def _incident_from_jsonl_line(self, line: str, users: "UserStore") -> Incident:
        """
        Converts one JSONL line into a ready-to-save Incident: json.loads()
        for the raw dict, then dispatches to the right concrete subclass's
        from_dict() based on the record's 'kind' field. This is the one
        place that needs to know which type to build from untyped file
        data - once built, the rest of the app (queues, API, persistence)
        handles Fault and MaintenanceTask polymorphically through the
        shared Incident type, never branching on 'kind' again.
        """
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError(f"each line must be a JSON object, got {type(data).__name__}")

        kind = data.get("kind")
        if kind not in ("maintenance", "fault"):
            raise ValueError(f"'kind' must be 'maintenance' or 'fault', got {kind!r}")

        created_by = self._resolve_seed_user(data.get("created_by"), users, "created_by")
        assigned_to_name = data.get("assigned_to")
        assigned_to = self._resolve_seed_user(assigned_to_name, users, "assigned_to") if assigned_to_name else None

        if kind == "fault":
            return Fault.from_dict(data, created_by=created_by, assigned_to=assigned_to)
        return MaintenanceTask.from_dict(data, created_by=created_by, assigned_to=assigned_to)

    @staticmethod
    def _resolve_seed_user(username: Optional[str], users: "UserStore", field_name: str) -> User:
        """
        Looks up a seed record's username against the real UserStore
        (built from INCIDENT_BRIDGE_USERS - see seed_users_from_env
        above). Deliberately stricter than app/persistence.py's restore
        path, which substitutes a "(removed)" placeholder for a username
        that no longer exists: that path is restoring data that was
        already valid when first written, while this one is validating
        brand-new seed data, so an unknown username here is treated as an
        invalid record instead of silently accepted.
        """
        if not username:
            raise ValueError(f"missing required field '{field_name}'")
        user = users.get(username)
        if user is None:
            raise ValueError(f"{field_name} {username!r} is not a known user (check INCIDENT_BRIDGE_USERS)")
        return user
