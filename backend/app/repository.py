"""In-memory stores: user accounts (backed by app/persistence_users.py), and every incident ever created."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional

from app.logs import app_log
from app.models import Fault, Incident, MaintenanceTask, ResolutionType, Role, User
from app.models.system_users import build_system_users, is_reserved_username
from app.persistence import SqliteIncidentStore
from app.persistence_users import SqliteUserStore


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
    Username -> User lookup, kept in memory for the life of the process.
    With a SqliteUserStore (the running app - see load_users() below) every
    added or saved user is also written to the database; without one (a
    plain UserStore, as in many tests and main.py) it is memory only.

    Explicit duplicate decision: usernames are unique, compared
    case-insensitively ("Admin" and "admin" would be two near-identical
    logins), and add() raises ValueError for a duplicate instead of silently
    replacing the existing user. Lookups with get() stay exact-case, so the
    login name is the username exactly as created.
    """

    def __init__(self, store: Optional[SqliteUserStore] = None):
        self._users: Dict[str, User] = {}
        self._store = store

    def __str__(self) -> str:
        return f"<UserStore: {len(self._users)} users>"

    def __repr__(self) -> str:
        """Developer view: the usernames (never passwords or hashes)."""
        return f"UserStore(usernames={sorted(self._users)!r})"

    def add(self, user: User, save: bool = True) -> None:
        """
        Add a user (and store it, if this UserStore has a database and
        `save` is True). Raises ValueError if the username is already taken
        (case-insensitive).
        """
        existing = self.find_case_insensitive(user.username)
        if existing is not None:
            raise ValueError(f"username {user.username!r} is already used by {existing.username!r}")
        self._users[user.username] = user
        if save:
            self.save(user)

    def save(self, user: User) -> None:
        """Write a changed user to the database (no-op without one, and for built-in system users)."""
        if self._store is not None and not is_reserved_username(user.username):
            self._store.save(user)

    def find_case_insensitive(self, username: str) -> Optional[User]:
        """The user whose username equals `username` ignoring letter case, or None."""
        wanted = username.lower()
        return next((u for u in self._users.values() if u.username.lower() == wanted), None)

    def get(self, username: str) -> Optional[User]:
        return self._users.get(username)

    def all_users(self) -> Iterator[User]:
        yield from self._users.values()

    def accounts(self) -> List[User]:
        """Every real account (no built-in system users), oldest first - the admin's user list."""
        return sorted((u for u in self._users.values() if not is_reserved_username(u.username)),
                      key=lambda u: (u.created_at, u.username))

def seed_users_from_env(env_var: str = "INCIDENT_BRIDGE_USERS", warn: bool = True) -> UserStore:
    """
    The REGULAR users listed in an environment variable holding a JSON array,
    e.g. (see .env.example):

        INCIDENT_BRIDGE_USERS=[{"username":"tech1","password":"Tech-Start1!","role":"user"},
                                {"username":"tech2","password":"Tech-Start2!","role":"user"}]

    Read once, by the setup page (app/api/setup.py), which creates them
    together with the first admin. Admins are never created from a file: the
    first one on the setup page, more on the Users page - so no admin
    password ever sits in .env. Unset or empty: no users (an empty store).
    `warn` is kept for callers that validate without creating.

    Fail-fast: every entry is checked and ALL problems are collected -
    invalid JSON, not a list, an entry that User.from_dict() rejects
    (missing field, bad role, weak password), an admin entry, a reserved
    name (a built-in system user such as "ai-assistant", case-insensitive)
    or a duplicate username (case-insensitive, naming the entry it
    repeats). If there is any problem, UserProvisioningError is raised with
    one message listing them all.

    Every built-in system user is added at the end (they can never log in).
    """
    store = UserStore()
    raw = os.environ.get(env_var)
    if not raw or not raw.strip():
        _add_system_users(store)
        return store

    def fail(problems: List[str]) -> UserProvisioningError:
        listed = "\n".join(f"  - {p}" for p in problems)
        return UserProvisioningError(
            f"{env_var} has {len(problems)} problem(s) - fix them before the setup:\n"
            f"{listed}\nFix the value in backend/.env (or your environment) and start again."
        )

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise fail([f"not valid JSON ({exc.msg} at position {exc.pos})"]) from None
    if not isinstance(entries, list):
        raise fail([f"must be a JSON array of users, got {type(entries).__name__}"])

    problems: List[str] = []
    first_entry_of: Dict[str, int] = {}          # lower-cased username -> entry number that defined it
    for number, entry in enumerate(entries, start=1):
        try:
            user = User.from_dict(entry)
        except ValueError as exc:
            problems.append(f"entry {number}: {exc}")
            continue
        if user.role == Role.ADMIN:
            problems.append(f"entry {number}: {user.username!r} is an admin - admins are not created from "
                            f"{env_var}: the first one on the setup page, others on the Users page")
            continue
        if is_reserved_username(user.username):
            problems.append(f"entry {number}: username {user.username!r} is reserved for a built-in "
                            f"system user - choose another name")
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
    _add_system_users(store)
    return store


def _add_system_users(store: UserStore) -> None:
    """Add every built-in system user (see app/models/system_users.py) to `store` (never to the database)."""
    for system_user in build_system_users():
        store.add(system_user, save=False)


def first_login_change_required() -> bool:
    """
    REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN (default true): whether a password
    chosen by someone else must be changed at the next login - the users
    created from INCIDENT_BRIDGE_USERS, accounts an admin creates on the
    Users page, and passwords an admin resets there (app/api/users.py).
    Read when the password is set: changing it later does not change
    accounts that already have the flag. Raises ValueError for a value that
    is not true/false.
    """
    raw = (os.environ.get("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN") or "true").strip().lower()
    if raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN must be true or false (got {raw!r})")
    return raw in ("true", "1", "yes")


def load_users(db: SqliteUserStore, warn: bool = True) -> UserStore:
    """
    The app's user accounts (called once by AppState.create()): every
    account in the users table - the table is the source of truth - plus
    the built-in system users, in memory. A new installation's table is
    empty until the setup page creates the first admin and the regular
    users from INCIDENT_BRIDGE_USERS (create_initial_users()).
    """
    store = UserStore(db)
    for user in db.load_all():
        store.add(user, save=False)
    _add_system_users(store)
    return store


def create_initial_users(users: UserStore, admin: User) -> List[User]:
    """
    The setup page's accounts: `admin` (who chose their own password) and
    the regular users of INCIDENT_BRIDGE_USERS, who must change their
    password at first login unless REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN
    is false. Validates everything before saving anything (raises
    UserProvisioningError or ValueError). Returns the regular users created.
    """
    regular = [u for u in seed_users_from_env(warn=False).all_users() if not is_reserved_username(u.username)]
    clash = next((u for u in regular if u.username.lower() == admin.username.lower()), None)
    if clash is not None:
        raise UserProvisioningError(f"INCIDENT_BRIDGE_USERS also lists {clash.username!r}, the admin's username "
                                    f"- remove it there or choose another admin name.")
    must_change = first_login_change_required()
    users.add(admin)
    for user in regular:
        user.must_change_password = must_change
        users.add(user)
    return regular


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
        assigned_to username, an invalid 'minutes_ago', 'comments' or
        'closed' entry (see _apply_seed_timeline() / _apply_seed_closure()), a line that is not a JSON object, or
        invalid JSON on that line is SKIPPED and recorded in the result with a
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
            incident = Fault.from_dict(data, created_by=created_by, assigned_to=assigned_to)
        else:
            incident = MaintenanceTask.from_dict(data, created_by=created_by, assigned_to=assigned_to)
        self._apply_seed_timeline(incident, data, users)
        self._apply_seed_closure(incident, data, users)
        return incident

    def _apply_seed_timeline(self, incident: Incident, data: dict, users: "UserStore") -> None:
        """
        Optional timing and update-thread fields of a seed record, so sample
        incidents can look like real ones that have been worked on for a
        while (useful for AI analysis, which reasons about the timeline):

            {"id": "seed-075", ..., "created_at": "2026-10-16T09:00:00+00:00",
             "comments": [
                {"author": "tech2", "text": "Restarted the worker.", "created_at": "2026-10-16T09:40:00Z"},
                {"author": "tech1", "text": "Latency is back."}
             ]}

        Every time is EITHER "created_at" - an ISO 8601 date and time WITH a
        time zone ("Z" or "+03:00"), not in the future - OR "minutes_ago",
        counted back from the moment of the import (the older form; both at
        once is an error). Imported now, an incident dated yesterday shows
        yesterday's time.

        - The record's time (optional): when it was reported. Without it,
          the incident is reported at import time - or, if it has older
          comments, at the time of its oldest comment, so a report is never
          newer than its own updates.
        - "comments" (optional): a list of updates, OLDEST FIRST. Each has
          an "author" (a known username - the same rule as created_by), a
          non-blank "text" (at most MAX_COMMENT_LENGTH characters - the
          normal Incident.add_comment() rules apply), and an optional time
          (default: the import time).

        Raises ValueError (so the loader skips the whole record with a
        reason) if any of these is invalid, if the comments are not in
        oldest-first order, or if a comment is older than the incident.
        The incident's updated_at becomes its newest comment's time.
        """
        now = datetime.now(timezone.utc)
        explicit_report_time = "minutes_ago" in data or "created_at" in data
        reported_at = self._seed_time(data, "created_at", now, "the record's")

        raw_comments = data.get("comments")
        if raw_comments is None:
            raw_comments = []
        if not isinstance(raw_comments, list):
            raise ValueError("'comments' must be a list of {author, text, minutes_ago} objects")

        previous_time = None
        for number, raw in enumerate(raw_comments, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"comment {number} must be a JSON object")
            author = self._resolve_seed_user(raw.get("author"), users, f"comment {number} 'author'")
            text = raw.get("text")
            if not isinstance(text, str):
                raise ValueError(f"comment {number} needs a 'text' string")
            posted_at = self._seed_time(raw, "created_at", now, f"comment {number}'s")
            if previous_time is not None and posted_at < previous_time:
                raise ValueError(f"comment {number} is older than the comment before it - list comments oldest first")
            if explicit_report_time and posted_at < reported_at:
                raise ValueError(f"comment {number} is older than the incident itself")
            try:
                comment = incident.add_comment(author, text)
            except ValueError as exc:
                raise ValueError(f"comment {number}: {exc}") from None
            comment.created_at = posted_at
            previous_time = posted_at

        if not explicit_report_time and incident.comments:
            reported_at = min(reported_at, incident.comments[0].created_at)
        incident.created_at = reported_at
        incident.updated_at = incident.comments[-1].created_at if incident.comments else reported_at

    def _apply_seed_closure(self, incident: Incident, data: dict, users: "UserStore") -> None:
        """
        Optional "closed" block of a seed record, so the sample data can
        include finished incidents (the dashboard's Archive tab):

            {"id": "seed-005", ..., "created_at": "2026-10-15T08:00:00Z",
             "closed": {"by": "tech1", "resolution": "resolved",
                        "message": "Fixed the template.", "closed_at": "2026-10-16T08:00:00Z"}}

        - "by": a known username; "resolution": resolved | not_an_incident |
          by_design; "message": the closing message. The normal
          Incident.close() rules apply (message required, Not an Incident
          and By Design are admin-only).
        - "closed_at" (ISO 8601 with a time zone, not in the future) or the
          older "minutes_ago" (optional, default: the import time): when it
          was closed - not older than the incident or its newest comment.

        The closing log comment and updated_at get that time. A closed
        record is not put in any queue (see import_jsonl in
        app/api/incidents.py). Raises ValueError (the record is skipped
        with a reason) for any invalid value.
        """
        closed = data.get("closed")
        if closed is None:
            return
        if not isinstance(closed, dict):
            raise ValueError("'closed' must be an object with by, resolution, message and an optional closed_at")
        actor = self._resolve_seed_user(closed.get("by"), users, "closed 'by'")
        raw_resolution = closed.get("resolution")
        try:
            resolution = ResolutionType(raw_resolution)
        except ValueError:
            allowed = ", ".join(r.value for r in ResolutionType)
            raise ValueError(f"closed 'resolution' must be one of: {allowed} (got {raw_resolution!r})") from None
        message = closed.get("message")
        if not isinstance(message, str):
            raise ValueError("closed needs a 'message' string")
        closed_at = self._seed_time(closed, "closed_at", datetime.now(timezone.utc), "closed")
        if closed_at < incident.updated_at:
            raise ValueError("the closing time is older than the incident or its newest comment")
        try:
            incident.close(actor, resolution, message)
        except (PermissionError, RuntimeError) as exc:
            raise ValueError(f"closed: {exc}") from None
        incident.comments[-1].created_at = closed_at
        incident.updated_at = closed_at

    # A clock difference of up to this much between the file's author and this
    # server is tolerated before a time counts as "in the future".
    _SEED_CLOCK_SKEW = timedelta(minutes=5)

    @classmethod
    def _seed_time(cls, obj: dict, key: str, now: datetime, owner: str) -> datetime:
        """
        The time given in `obj` as `key` (an ISO 8601 date-time with a time
        zone) or as "minutes_ago"; `now` when neither is there. `owner`
        names it in errors. Raises ValueError for both at once, a bad or
        zone-less value, or a time in the future.
        """
        if key in obj and "minutes_ago" in obj:
            raise ValueError(f"{owner} time: give '{key}' or 'minutes_ago', not both")
        if key not in obj:
            return now - timedelta(minutes=cls._seed_minutes_ago(obj.get("minutes_ago", 0), owner))
        raw = obj[key]
        try:
            # fromisoformat() before Python 3.11 does not read a trailing "Z".
            moment = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"{owner} '{key}' must be an ISO 8601 date and time, "
                             f"e.g. 2026-10-16T14:30:00+00:00 (got {raw!r})") from None
        if moment.tzinfo is None:
            raise ValueError(f"{owner} '{key}' needs a time zone, e.g. 'Z' or '+03:00' (got {raw!r})")
        if moment > now + cls._SEED_CLOCK_SKEW:
            raise ValueError(f"{owner} '{key}' is in the future (got {raw!r})")
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _seed_minutes_ago(value, owner: str) -> float:
        """Validate a 'minutes_ago' value: a number of minutes, 0 or more. `owner` names it in the error."""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{owner} 'minutes_ago' must be a number of minutes, 0 or more (got {value!r})")
        return float(value)

    @staticmethod
    def _resolve_seed_user(username: Optional[str], users: "UserStore", field_name: str) -> User:
        """
        Looks up a seed record's username against the real UserStore (the
        accounts in the database). Deliberately stricter than app/persistence.py's restore
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
            raise ValueError(f"{field_name} {username!r} is not an account here - create it on the Users page "
                             f"(or choose that username at the setup), then import again")
        return user
