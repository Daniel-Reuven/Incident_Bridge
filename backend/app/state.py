"""
AppState: the single in-memory state container for one running process.

Confirmed constraint (see backend/README.md and the project's hosting
notes): this app must run as a single process / single worker, because
this state - the queues especially - is plain in-memory Python and is NOT
shared across multiple processes. Running multiple Uvicorn workers would
give each one its own separate queues, silently breaking the "one shared
FIFO" and "one shared priority queue" guarantees.

Incidents (and their comments) persist across restarts via SQLite - see
app/persistence.py. Users do not persist here; they stay env-provisioned
via seed_users_from_env, exactly as before.

Sites, mailing lists and notifications (the Sites & Mailing Lists
subsystem) live in a SiteDirectory backed by its own SqliteSiteStore (same
database file, separate tables - see app/persistence_sites.py). At startup
the stored data is loaded first and the JSONL seed files - only those
configured in SITES_SEED_PATH / MAILING_LISTS_SEED_PATH - are imported after
it, so seed records whose ids are already stored are skipped: edits made in
the app survive restarts and archived sites are never brought back.
"""

import os
from dataclasses import dataclass
from typing import List

from app.models import Fault, IncidentStatus, MaintenanceTask
from app.persistence import SqliteIncidentStore
from app.persistence_sites import SqliteSiteStore
from app.queues import FaultPriorityQueue, MaintenanceQueueManager
from app.repository import IncidentRepository, UserStore, seed_users_from_env
from app.services.availability import HttpChecker
from app.services.notifier import OutboxNotifier
from app.sites import SiteDirectory, seed_site_directory


def order_pending_maintenance(tasks: List[MaintenanceTask], has_current: bool) -> List[MaintenanceTask]:
    """
    The order in which persisted OPEN maintenance tasks go back into the
    FIFO queue at startup.

    Normally that is their saved queue_position, which the API layer
    persists after every queue change (closing a task from the middle
    shifts the ones behind it, so creation order alone can no longer
    describe the queue). But those saved positions are only trusted when
    they form EXACTLY the sequence a live queue would have: 1..N, or
    2..N+1 when a task is in progress (position 1 belongs to it).
    Anything else - a gap, a duplicate, a missing value - means the data
    predates position persistence or is otherwise stale, and the safe
    fallback is creation order, which is what the app used before. The
    first queue change after that re-saves correct positions for every
    task, so the fallback only ever applies once.
    """
    start = 2 if has_current else 1
    expected = list(range(start, start + len(tasks)))
    saved = sorted(task.queue_position for task in tasks if task.queue_position is not None)
    if len(saved) == len(tasks) and saved == expected:
        return sorted(tasks, key=lambda task: task.queue_position)
    return sorted(tasks, key=lambda task: task.created_at)


@dataclass
class AppState:
    users: UserStore
    incidents: IncidentRepository
    maintenance: MaintenanceQueueManager
    faults: FaultPriorityQueue
    sites: SiteDirectory

    @classmethod
    def create(cls) -> "AppState":
        """
        Build state for this process - called once at app startup (see
        app/api/app.py's lifespan). Loads any previously-persisted
        incidents and re-populates the queues so a restart doesn't lose
        in-flight work, not just closed history. Then builds the
        SiteDirectory: stored sites/lists/notifications first, then the
        seed files, but only those configured in SITES_SEED_PATH /
        MAILING_LISTS_SEED_PATH (unset or empty = no import - see
        app/sites.py), whose already-stored ids are skipped. The directory
        gets an HttpChecker configured from SITE_CHECK_TIMEOUT_SECONDS /
        SITE_CHECK_SLOW_MS for availability checks, and an OutboxNotifier
        that records sent notifications in the app (no real email).
        """
        users = seed_users_from_env()

        db_path = os.environ.get("DATABASE_PATH", "incident_bridge.db")
        store = SqliteIncidentStore(db_path)
        incidents = IncidentRepository(store)
        maintenance = MaintenanceQueueManager()
        faults = FaultPriorityQueue()

        persisted = store.load_all(users)
        incidents.bulk_load(persisted)

        # Route each loaded incident back into the right place, mirroring
        # the exact invariants the running app maintains during normal
        # operation: OPEN maintenance tasks are pending, at most one
        # IN_PROGRESS one is current, OPEN faults sit in the priority
        # queue, and anything else (IN_PROGRESS faults - already claimed -
        # and anything CLOSED) lives in the repository only, same as at
        # runtime. `persisted` is ordered by created_at (see
        # SqliteIncidentStore.load_all), which is the order OPEN faults
        # are pushed in, so their tiebreak order comes back as it was.
        #
        # Pending maintenance tasks are NOT replayed in created_at order:
        # a task closed from the middle of the queue, or (in a later step)
        # reopened at a chosen position, means creation order no longer
        # describes the FIFO - see order_pending_maintenance().
        default_queue = maintenance.get_queue()
        maintenance_tasks = [i for i in persisted if isinstance(i, MaintenanceTask)]

        has_current = False
        for task in maintenance_tasks:
            if task.status == IncidentStatus.IN_PROGRESS:
                default_queue.restore_current(task)
                has_current = True

        pending = [task for task in maintenance_tasks if task.status == IncidentStatus.OPEN]
        for task in order_pending_maintenance(pending, has_current):
            default_queue.enqueue(task)

        for incident in persisted:
            if isinstance(incident, Fault) and incident.status == IncidentStatus.OPEN:
                faults.push(incident)

        site_store = SqliteSiteStore(db_path)
        # HttpChecker.from_env() raises ValueError (stopping startup with a
        # clear message) if SITE_CHECK_TIMEOUT_SECONDS / SITE_CHECK_SLOW_MS
        # hold something that is not a positive number.
        sites = SiteDirectory(site_store, checker=HttpChecker.from_env(), notifier=OutboxNotifier())
        sites.bulk_load(*site_store.load_all())
        seed_site_directory(sites)

        return cls(users=users, incidents=incidents, maintenance=maintenance, faults=faults, sites=sites)
