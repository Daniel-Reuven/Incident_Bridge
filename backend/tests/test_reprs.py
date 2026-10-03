"""
Tests for the developer-facing __repr__ of the queues, the queue manager,
IncidentWorkSession and the stores.

Run from backend/:  python -m pytest tests/test_reprs.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

from app.context import IncidentWorkSession
from app.models import Fault, MaintenanceTask, Role, SeverityCategory, User
from app.persistence import SqliteIncidentStore
from app.queues import FaultPriorityQueue, MaintenanceQueue, MaintenanceQueueManager
from app.repository import IncidentRepository, UserStore


def test_maintenance_queue_repr(admin_user):
    queue = MaintenanceQueue()
    assert repr(queue) == "MaintenanceQueue(name='default', current=None, pending=0)"
    first = MaintenanceTask("A", "d", admin_user)
    queue.enqueue(first)
    queue.enqueue(MaintenanceTask("B", "d", admin_user))
    queue.start_next()
    assert repr(queue) == f"MaintenanceQueue(name='default', current='{first.id[:8]}', pending=1)"


def test_manager_repr(admin_user):
    manager = MaintenanceQueueManager()
    manager.get_queue().enqueue(MaintenanceTask("A", "d", admin_user))
    manager.get_queue("network")
    assert repr(manager) == "MaintenanceQueueManager(queues={'default': 1, 'network': 0})"


def test_fault_queue_repr(admin_user):
    queue = FaultPriorityQueue()
    assert repr(queue) == "FaultPriorityQueue(size=0, head=None)"
    critical = Fault("Down", "d", admin_user, SeverityCategory.CRITICAL, 1.0)
    queue.push(Fault("Typo", "d", admin_user, SeverityCategory.MINOR, 3.0))
    queue.push(critical)
    assert repr(queue) == f"FaultPriorityQueue(size=2, head='{critical.id[:8]}')"


def test_work_session_repr(admin_user):
    task = MaintenanceTask("A", "d", admin_user)
    session = IncidentWorkSession(task, actor=admin_user)
    assert repr(session) == f"IncidentWorkSession(incident={task!r}, actor='test-admin', started=False)"
    with session:
        assert repr(session).endswith("started=True)")


def test_store_reprs_never_show_secrets():
    users = UserStore()
    users.add(User("tech1", Role.USER, "Passw0rd2"))
    users.add(User("admin", Role.ADMIN, "Passw0rd1"))
    assert repr(users) == "UserStore(usernames=['admin', 'tech1'])"
    assert "Passw0rd" not in repr(users)
    store = SqliteIncidentStore(":memory:")
    assert repr(store) == "SqliteIncidentStore(db_path=':memory:')"
    assert repr(IncidentRepository(store)) == "IncidentRepository(incidents=0, store=SqliteIncidentStore(db_path=':memory:'))"
    assert repr(IncidentRepository()) == "IncidentRepository(incidents=0, store=None)"
