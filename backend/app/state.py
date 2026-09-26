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
"""

import os
from dataclasses import dataclass

from app.models import Fault, IncidentStatus, MaintenanceTask
from app.persistence import SqliteIncidentStore
from app.queues import FaultPriorityQueue, MaintenanceQueueManager
from app.repository import IncidentRepository, UserStore, seed_users_from_env


@dataclass
class AppState:
    users: UserStore
    incidents: IncidentRepository
    maintenance: MaintenanceQueueManager
    faults: FaultPriorityQueue

    @classmethod
    def create(cls) -> "AppState":
        """
        Build state for this process - called once at app startup (see
        app/api/app.py's lifespan). Loads any previously-persisted
        incidents and re-populates the queues so a restart doesn't lose
        in-flight work, not just closed history.
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
        # runtime. `persisted` is already ordered by created_at (see
        # SqliteIncidentStore.load_all), so FIFO order and the fault
        # queue's tiebreak order both come back exactly as they were.
        default_queue = maintenance.get_queue()
        for incident in persisted:
            if isinstance(incident, MaintenanceTask):
                if incident.status == IncidentStatus.IN_PROGRESS:
                    default_queue.restore_current(incident)
                elif incident.status == IncidentStatus.OPEN:
                    default_queue.enqueue(incident)
            elif isinstance(incident, Fault):
                if incident.status == IncidentStatus.OPEN:
                    faults.push(incident)

        return cls(users=users, incidents=incidents, maintenance=maintenance, faults=faults)
