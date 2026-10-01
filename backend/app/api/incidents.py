"""
Incident endpoints: maintenance (FIFO) and fault (priority queue)
operations, plus shared listing, detail, and comment endpoints.

Permission checks (admin-only severity changes, admin-only resolution
types) are enforced inside the domain layer (app/models), not duplicated
here - this router just calls straight into the domain methods and lets
app/api/app.py's exception handlers turn PermissionError/ValueError/etc.
into the right HTTP status.

Every mutating endpoint calls state.incidents.save(...) (persists to
SQLite - app/persistence.py) and then broadcaster.publish(...) (live
update to every other connected browser tab - app/events.py), in that
order, right after the domain-level mutation succeeds. If either of
those raised before the response is built, the client would see a 5xx
for a change that actually went through - out of scope to fix at this
project's scale, but worth knowing.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_broadcaster, get_client_id, get_current_user, get_state
from app.api.schemas import (
    AddCommentRequest,
    ChangeSeverityRequest,
    CloseFaultRequest,
    CompleteMaintenanceRequest,
    CreateFaultRequest,
    CreateMaintenanceTaskRequest,
    ImportJsonlRequest,
)
from app.api.serializers import incident_to_dict
from app.events import EventBroadcaster
from app.models import Fault, MaintenanceTask, Role, User
from app.services import SeverityScorer
from app.state import AppState

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _publish(broadcaster: EventBroadcaster, client_id: Optional[str], incident, action: str, actor: User) -> None:
    """Shared shape for every live-update event - see frontend/js/toast.js's describeEvent for the consumer."""
    broadcaster.publish(
        {
            "type": "fault" if isinstance(incident, Fault) else "maintenance",
            "id": incident.id,
            "action": action,
            "actor": actor.username,
            "title": incident.title,
        },
        exclude_client_id=client_id,
    )


# --- shared: listing, detail, comments (both incident types) ---

@router.get("")
def list_incidents(
        type: Optional[str] = None,
        status: Optional[str] = None,
        state: AppState = Depends(get_state),
        _current_user: User = Depends(get_current_user),
):
    """List every incident, optionally filtered by type ('maintenance'/'fault') and/or status."""
    items = state.incidents.list_all()
    if type:
        items = (i for i in items if ("fault" if isinstance(i, Fault) else "maintenance") == type)
    if status:
        items = (i for i in items if i.status.value == status)
    return [incident_to_dict(i) for i in items]


@router.get("/{incident_id}")
def get_incident(incident_id: str, state: AppState = Depends(get_state),
                 _current_user: User = Depends(get_current_user)):
    return incident_to_dict(state.incidents.get(incident_id))


@router.post("/{incident_id}/comments")
def add_comment(incident_id: str, body: AddCommentRequest, state: AppState = Depends(get_state),
                current_user: User = Depends(get_current_user),
                broadcaster: EventBroadcaster = Depends(get_broadcaster),
                client_id: Optional[str] = Depends(get_client_id)):
    """Any authenticated user may add a comment/update to any incident - including a closed one."""
    incident = state.incidents.get(incident_id)
    incident.add_comment(current_user, body.text)
    state.incidents.save(incident)
    _publish(broadcaster, client_id, incident, "commented", current_user)
    return incident_to_dict(incident)


# --- bulk import (admin only) ---

@router.post("/import-jsonl")
def import_jsonl(body: ImportJsonlRequest, state: AppState = Depends(get_state),
                 current_user: User = Depends(get_current_user),
                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                 client_id: Optional[str] = Depends(get_client_id)):
    """
    Admin-only bulk import of synthetic seed incidents from JSONL text
    (see app/repository.py's IncidentRepository.load_from_jsonl_lines,
    which does the actual parsing/validation/dedup - this endpoint's job
    is everything load_from_jsonl_lines deliberately does NOT do:
    enqueueing each new incident into the real, live queue it belongs in,
    and broadcasting it, exactly like create_maintenance_task/create_fault
    below do for a single incident created through the normal dashboard
    dialog. This is what makes an import show up live, with no restart -
    unlike backend/seed_from_jsonl.py's original approach of writing
    directly to the database from a separate, short-lived process, which
    had no way to reach this already-running process's in-memory queues.

    Takes raw JSONL text in the request body rather than a server-side
    file path - see ImportJsonlRequest's docstring for why - so this same
    endpoint works whether it's triggered by the seed_from_jsonl.py CLI
    script (reads a local file, sends its content) or a future
    browser-side file picker on the dashboard (has no server-side path to
    give it, only a file's contents).
    """
    if current_user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Only an admin may import seed data.")

    result = state.incidents.load_from_jsonl_lines(body.content.splitlines(), state.users)

    for incident_id in result.created_ids:
        incident = state.incidents.get(incident_id)
        if isinstance(incident, Fault):
            state.faults.push(incident)
        else:
            state.maintenance.get_queue().enqueue(incident)
        _publish(broadcaster, client_id, incident, "created", current_user)

    return {
        "created": result.created,
        "created_ids": result.created_ids,
        "skipped_duplicate_ids": result.skipped_duplicate_ids,
        "skipped_invalid": result.skipped_invalid,
    }


# --- maintenance: strict, immutable FIFO ---

@router.get("/maintenance/queue")
def get_maintenance_queue(state: AppState = Depends(get_state), _current_user: User = Depends(get_current_user)):
    queue = state.maintenance.get_queue()
    return {
        "current": incident_to_dict(queue.current_task) if queue.current_task else None,
        "pending": [incident_to_dict(t) for t in queue],
    }


@router.post("/maintenance")
def create_maintenance_task(body: CreateMaintenanceTaskRequest, state: AppState = Depends(get_state),
                            current_user: User = Depends(get_current_user),
                            broadcaster: EventBroadcaster = Depends(get_broadcaster),
                            client_id: Optional[str] = Depends(get_client_id)):
    task = MaintenanceTask(title=body.title, description=body.description, created_by=current_user)
    state.incidents.add(task)
    state.maintenance.get_queue().enqueue(task)
    state.incidents.save(task)
    _publish(broadcaster, client_id, task, "created", current_user)
    return incident_to_dict(task)


@router.post("/maintenance/start-next")
def start_next_maintenance(state: AppState = Depends(get_state), current_user: User = Depends(get_current_user),
                           broadcaster: EventBroadcaster = Depends(get_broadcaster),
                           client_id: Optional[str] = Depends(get_client_id)):
    """Raises 409 (via RuntimeError) if a task is already in progress, or 404 (IndexError) if the queue is empty."""
    task = state.maintenance.get_queue().start_next()
    state.incidents.save(task)
    _publish(broadcaster, client_id, task, "started", current_user)
    return incident_to_dict(task)


@router.post("/maintenance/complete-current")
def complete_current_maintenance(body: CompleteMaintenanceRequest, state: AppState = Depends(get_state),
                                 current_user: User = Depends(get_current_user),
                                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                                 client_id: Optional[str] = Depends(get_client_id)):
    task = state.maintenance.get_queue().complete_current(
        actor=current_user, message=body.message, resolution_type=body.resolution_type
    )
    state.incidents.save(task)
    _publish(broadcaster, client_id, task, "closed", current_user)
    return incident_to_dict(task)


# --- faults: single shared priority queue, every severity ---

@router.get("/faults/queue")
def get_fault_queue(state: AppState = Depends(get_state), _current_user: User = Depends(get_current_user)):
    """Faults still waiting to be claimed, in priority order (Critical first, all severities together)."""
    return [incident_to_dict(f) for f in state.faults]


@router.post("/faults")
def create_fault(body: CreateFaultRequest, state: AppState = Depends(get_state),
                 current_user: User = Depends(get_current_user),
                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                 client_id: Optional[str] = Depends(get_client_id)):
    severity = SeverityScorer.score(body.details)
    fault = Fault(
        title=body.title, description=body.description, created_by=current_user,
        severity=severity, severity_score=float(severity.value), details=body.details,
    )
    state.incidents.add(fault)
    state.faults.push(fault)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "created", current_user)
    return incident_to_dict(fault)


@router.post("/faults/claim-next")
def claim_next_fault(state: AppState = Depends(get_state), current_user: User = Depends(get_current_user),
                     broadcaster: EventBroadcaster = Depends(get_broadcaster),
                     client_id: Optional[str] = Depends(get_client_id)):
    """Pop the most severe fault off the queue and mark it in progress, assigned to the caller."""
    fault = state.faults.pop_most_severe()
    fault.start_progress()
    fault.assigned_to = current_user
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "claimed", current_user)
    return incident_to_dict(fault)


@router.patch("/faults/{incident_id}/severity")
def change_fault_severity(incident_id: str, body: ChangeSeverityRequest, state: AppState = Depends(get_state),
                          current_user: User = Depends(get_current_user),
                          broadcaster: EventBroadcaster = Depends(get_broadcaster),
                          client_id: Optional[str] = Depends(get_client_id)):
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(status_code=400, detail="Only faults have a severity.")
    fault.change_severity(current_user, body.severity)  # admin-only, enforced in the domain layer
    # heapq has no in-place "update priority" - reprioritize() removes and
    # re-pushes the fault so the heap actually reflects the new severity.
    # A no-op (returns False) if the fault isn't currently queued, e.g. it
    # was already claimed via /faults/claim-next.
    state.faults.reprioritize(fault)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "severity_changed", current_user)
    return incident_to_dict(fault)


@router.post("/faults/{incident_id}/close")
def close_fault(incident_id: str, body: CloseFaultRequest, state: AppState = Depends(get_state),
                current_user: User = Depends(get_current_user),
                broadcaster: EventBroadcaster = Depends(get_broadcaster),
                client_id: Optional[str] = Depends(get_client_id)):
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(
            status_code=400,
            detail="Maintenance tasks are closed via /incidents/maintenance/complete-current, not this endpoint.",
        )
    fault.close(current_user, body.resolution_type, body.message)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "closed", current_user)
    return incident_to_dict(fault)
