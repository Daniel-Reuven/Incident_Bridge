"""
Incident endpoints: maintenance (FIFO) and fault (priority queue)
operations, plus shared listing, detail, and comment endpoints.

Permission checks (admin-only severity changes, admin-only resolution
types, admin-only reopening) are enforced inside the domain layer
(app/models), not duplicated here - this router just calls straight into
the domain methods and lets app/api/app.py's exception handlers turn
PermissionError/ValueError/etc. into the right HTTP status.

Every mutating endpoint calls state.incidents.save(...) (persists to
SQLite - app/persistence.py) and then broadcaster.publish(...) (live
update to every other connected browser tab - app/events.py), in that
order, right after the domain-level mutation succeeds. If either of
those raised before the response is built, the client would see a 5xx
for a change that actually went through - out of scope to fix at this
project's scale, but worth knowing.

Every status change leaves a log comment on the incident saying who did
it (Incident.log_status_change): close() and reopen() write theirs inside
the domain layer, and Fault.claim() / the maintenance start endpoints
write theirs.

Maintenance endpoints that change the queue additionally persist EVERY
task in it (see _persist_maintenance_queue), because a change to one
task can shift the queue_position of the others, and the startup replay
in app/state.py rebuilds the FIFO from those saved positions.
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_broadcaster, get_client_id, get_current_user, get_state
from app.api.schemas import (
    AddCommentRequest,
    ChangeSeverityRequest,
    CloseFaultRequest,
    CompleteMaintenanceRequest,
    CreateFaultRequest,
    CreateMaintenanceTaskRequest,
    ImportJsonlRequest,
    ReopenFaultRequest,
    ReopenMaintenanceRequest,
)
from app.api.serializers import incident_to_dict
from app.events import EventBroadcaster
from app.models import Fault, IncidentStatus, MaintenanceTask, Role, User
from app.iterators import first_pressing_calls
from app.services import SeverityScorer
from app.state import AppState

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _publish(broadcaster: EventBroadcaster, client_id: Optional[str], incident, action: str, actor: User,
             affected_ids: Optional[List[str]] = None) -> None:
    """
    Shared shape for every live-update event - see frontend/js/toast.js's
    describeEvent for the consumer. `affected_ids` is optional: the ids of
    OTHER incidents whose state this action changed (e.g. maintenance tasks
    whose queue position shifted when one ahead of them was closed), so a
    browser tab showing one of those can offer a refresh. Left out of the
    event entirely when empty.
    """
    event = {
        "type": "fault" if isinstance(incident, Fault) else "maintenance",
        "id": incident.id,
        "action": action,
        "actor": actor.username,
        "title": incident.title,
    }
    if affected_ids:
        event["affected_ids"] = affected_ids
    broadcaster.publish(event, exclude_client_id=client_id)


def _persist_maintenance_queue(state: AppState, queue) -> None:
    """
    Save every task currently in `queue` (the in-progress one and all
    pending ones). Call after ANY change to the queue: positions shift for
    tasks other than the one the request was about, and each task's saved
    queue_position is what the startup replay orders the FIFO by.
    A task that just LEFT the queue (closed) is no longer in it, so the
    caller saves that one separately.
    """
    state.incidents.save_all(queue.all_tasks())


def _queue_positions(queue) -> Dict[str, Optional[int]]:
    """Snapshot of {task id: queue_position} - take one BEFORE a queue change to diff against afterwards."""
    return {task.id: task.queue_position for task in queue.all_tasks()}


def _shifted_ids(before: Dict[str, Optional[int]], queue) -> List[str]:
    """Ids of tasks still in `queue` whose position differs from the `before` snapshot, in queue order."""
    return [task.id for task in queue.all_tasks() if task.id in before and before[task.id] != task.queue_position]


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

    Once everything is enqueued, the whole maintenance queue is persisted
    so the newly assigned queue positions are saved too.
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

    _persist_maintenance_queue(state, state.maintenance.get_queue())

    return {
        "created": result.created,
        "created_ids": result.created_ids,
        "skipped_duplicate_ids": result.skipped_duplicate_ids,
        "skipped_invalid": result.skipped_invalid,
    }


# --- maintenance: strict FIFO (never reordered; closed from or reopened into any position) ---

@router.get("/maintenance/queue")
def get_maintenance_queue(state: AppState = Depends(get_state), _current_user: User = Depends(get_current_user)):
    queue = state.maintenance.get_queue()
    return {
        "current": incident_to_dict(queue.current_task) if queue.current_task else None,
        "pending": [incident_to_dict(t) for t in queue],
    }


@router.get("/maintenance/pressing")
def get_pressing_maintenance(limit: Optional[int] = Query(None, ge=1, le=500), state: AppState = Depends(get_state),
                             _current_user: User = Depends(get_current_user)):
    """
    Open maintenance calls that have waited more than 3 days (in-progress
    and closed calls are left out), produced by the lazy pipeline in
    app/iterators.py. With `limit`, the pipeline stops after that many
    results; `examined` shows how many incidents it actually had to look
    at, and `stopped_early` is True when some were never touched at all.
    """
    calls, examined = first_pressing_calls(state.incidents.list_all(), limit)
    total = sum(1 for _ in state.incidents.list_all())
    return {
        "limit": limit,
        "results": calls,
        "examined": examined,
        "total_incidents": total,
        "stopped_early": examined < total,
    }


@router.get("/maintenance/reopen-options")
def get_maintenance_reopen_options(state: AppState = Depends(get_state),
                                   _current_user: User = Depends(get_current_user)):
    """
    What the "reopen a maintenance task" dialog needs to know about the
    queue right now, so it can offer only valid choices:
      - first_position / last_position: the lowest and highest position a
        reopened task may be given (with a task in progress, position 1 is
        taken, so first_position is 2);
      - has_current: whether a task is in progress;
      - can_start: whether "In progress" can be offered at all (nothing is
        in progress; it is further limited to position 1 by the dialog);
      - queue_empty: whether the queue has no tasks, in which case the
        dialog skips the position question entirely.
    This is only a convenience for the UI - POST .../reopen re-validates
    everything, since the queue may change between the two calls.
    """
    queue = state.maintenance.get_queue()
    first, last = queue.placement_bounds()
    return {
        "has_current": queue.current_task is not None,
        "first_position": first,
        "last_position": last,
        "can_start": queue.current_task is None,
        "queue_empty": len(queue) == 0,
    }


@router.post("/maintenance")
def create_maintenance_task(body: CreateMaintenanceTaskRequest, state: AppState = Depends(get_state),
                            current_user: User = Depends(get_current_user),
                            broadcaster: EventBroadcaster = Depends(get_broadcaster),
                            client_id: Optional[str] = Depends(get_client_id)):
    task = MaintenanceTask(title=body.title, description=body.description, created_by=current_user)
    state.incidents.add(task)
    queue = state.maintenance.get_queue()
    queue.enqueue(task)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "created", current_user)
    return incident_to_dict(task)


@router.post("/maintenance/start-next")
def start_next_maintenance(state: AppState = Depends(get_state), current_user: User = Depends(get_current_user),
                           broadcaster: EventBroadcaster = Depends(get_broadcaster),
                           client_id: Optional[str] = Depends(get_client_id)):
    """
    Start whichever task is first in the queue right now (the dashboard's
    "Start next" button). Raises 409 (via RuntimeError) if a task is already
    in progress, or 404 (IndexError) if the queue is empty. Writes the
    status-change log comment ("Status changed from Open to In progress."),
    authored by whoever pressed the button - the same entry the per-task
    Start button writes (see start_maintenance_task).
    """
    queue = state.maintenance.get_queue()
    task = queue.start_next()
    task.log_status_change(current_user, IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "started", current_user)
    return incident_to_dict(task)


@router.post("/maintenance/{incident_id}/start")
def start_maintenance_task(incident_id: str, state: AppState = Depends(get_state),
                           current_user: User = Depends(get_current_user),
                           broadcaster: EventBroadcaster = Depends(get_broadcaster),
                           client_id: Optional[str] = Depends(get_client_id)):
    """
    Start one specific maintenance task - the Start button on the task's own
    page. Unlike /maintenance/start-next (which starts whatever is first
    right now), this only succeeds if THIS task is still the first pending
    one and nothing is in progress (MaintenanceQueue.start_task), so a page
    that has gone stale can never start a different task than the one shown.

    409 if a task is already in progress, if this task is no longer first, or
    if it is not in a queue (e.g. already closed). 400 for a fault, 404 for an
    unknown id. Writes the same status-change log comment as other manual
    status changes ("Status changed from Open to In progress."), authored by
    whoever pressed Start.
    """
    task = state.incidents.get(incident_id)
    if not isinstance(task, MaintenanceTask):
        raise HTTPException(
            status_code=400,
            detail="Only maintenance tasks are started this way - faults are claimed instead.",
        )
    queue = state.maintenance.queue_of(task)
    if queue is None:
        raise RuntimeError("This maintenance task is not in a queue (it may already be closed).")

    queue.start_task(task)
    task.log_status_change(current_user, IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "started", current_user)
    return incident_to_dict(task)


@router.post("/maintenance/complete-current")
def complete_current_maintenance(body: CompleteMaintenanceRequest, state: AppState = Depends(get_state),
                                 current_user: User = Depends(get_current_user),
                                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                                 client_id: Optional[str] = Depends(get_client_id)):
    """
    Close the in-progress task. Every pending task moves up one position, so
    the whole queue is persisted and the shifted tasks are reported in the
    live-update event's affected_ids.
    """
    queue = state.maintenance.get_queue()
    before = _queue_positions(queue)
    task = queue.complete_current(
        actor=current_user, message=body.message, resolution_type=body.resolution_type
    )
    state.incidents.save(task)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "closed", current_user, affected_ids=_shifted_ids(before, queue))
    return incident_to_dict(task)


@router.post("/maintenance/{incident_id}/close")
def close_maintenance_task(incident_id: str, body: CompleteMaintenanceRequest, state: AppState = Depends(get_state),
                           current_user: User = Depends(get_current_user),
                           broadcaster: EventBroadcaster = Depends(get_broadcaster),
                           client_id: Optional[str] = Depends(get_client_id)):
    """
    Close ANY maintenance task in the queue, whatever its position - pending
    from anywhere in the middle, or the in-progress one. The message is the
    required reason (400 if blank); any role may close, but the admin-only
    resolution types (Not an Incident / By Design) are still enforced by the
    domain layer (403). Incident.close() writes the log comment.

    The tasks behind a closed pending task each move up one place; the whole
    queue is persisted so those positions survive a restart, and the shifted
    tasks are listed in the live-update event's affected_ids so a tab showing
    one of them can offer a refresh. Closing the in-progress task leaves the
    next one Open until Start next is pressed.

    400 for a fault (faults have their own close endpoint), 404 for an
    unknown id, 409 if the task is not in a queue (e.g. already closed).
    """
    task = state.incidents.get(incident_id)
    if not isinstance(task, MaintenanceTask):
        raise HTTPException(
            status_code=400,
            detail="Faults are closed via /incidents/faults/{id}/close, not this endpoint.",
        )
    queue = state.maintenance.queue_of(task)
    if queue is None:
        raise RuntimeError("This maintenance task is not in a queue (it may already be closed).")

    before = _queue_positions(queue)
    queue.close_task(task, current_user, body.message, body.resolution_type)
    state.incidents.save(task)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "closed", current_user, affected_ids=_shifted_ids(before, queue))
    return incident_to_dict(task)


@router.post("/maintenance/{incident_id}/reopen")
def reopen_maintenance_task(incident_id: str, body: ReopenMaintenanceRequest, state: AppState = Depends(get_state),
                            current_user: User = Depends(get_current_user),
                            broadcaster: EventBroadcaster = Depends(get_broadcaster),
                            client_id: Optional[str] = Depends(get_client_id)):
    """
    Reopen a closed maintenance task as Open or In progress and put it back
    into the FIFO queue at the requested position. Admin-only (403 for a
    regular user), reason required (400 if blank), 409 if the task is not
    currently closed, 400 if the id belongs to a fault.

    `position` uses the displayed numbering (the in-progress task is
    position 1); omit it to put the task at the back. Placement rules (see
    MaintenanceQueue.check_placement): only position 1 can be In progress,
    and only while nothing else is (409 otherwise); the position must be
    within the queue's current bounds (400 otherwise).

    Order of operations: the placement is validated FIRST, while the task is
    still untouched, so a bad position never leaves a half-reopened task.
    Then Incident.reopen() runs (permission, reason, state checks; clears the
    resolution; writes the log comment including "Queue position: N"), then
    the task is inserted. The whole queue is persisted so every shifted
    position survives a restart, and the tasks that moved down are listed in
    the live-update event's affected_ids.
    """
    task = state.incidents.get(incident_id)
    if not isinstance(task, MaintenanceTask):
        raise HTTPException(status_code=400, detail="Only maintenance tasks can be reopened via this endpoint.")

    queue = state.maintenance.get_queue()
    position = body.position if body.position is not None else queue.placement_bounds()[1]
    queue.check_placement(body.status, position)

    before = _queue_positions(queue)
    task.reopen(current_user, body.status, body.reason, detail=f"Queue position: {position}")
    queue.reinsert(task, position)
    _persist_maintenance_queue(state, queue)
    _publish(broadcaster, client_id, task, "reopened", current_user, affected_ids=_shifted_ids(before, queue))
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
    """
    Claim the most severe fault in the queue (the dashboard's "Claim next"
    button): it becomes In progress, assigned to the caller, with the
    status-change log comment ("Status changed from Open to In progress.
    Assigned to <user>.") written by Fault.claim().

    peek() -> claim() -> remove(), in that order, so the fault only leaves
    the queue once the domain claim has succeeded. 404 (IndexError) if the
    queue is empty. Unlike /faults/{id}/claim, no priority check is needed:
    the fault claimed here is by definition the most severe one waiting.
    """
    fault = state.faults.peek()
    fault.claim(current_user)
    state.faults.remove(fault)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "claimed", current_user)
    return incident_to_dict(fault)


@router.get("/faults/{incident_id}/claim-status")
def get_fault_claim_status(incident_id: str, state: AppState = Depends(get_state),
                           _current_user: User = Depends(get_current_user)):
    """
    Whether this fault could be claimed from its page right now, so the
    incident page can enable/disable its Claim button and show the
    "N higher-priority faults still unclaimed" tooltip. Deliberately its
    own small endpoint (not folded into GET /incidents/{id}) so the page can
    re-check it on every live-update event without re-fetching and
    re-rendering the whole incident - which would wipe whatever the user
    is typing.

    Returns {"queued": bool, "claimable": bool, "higher_priority_unclaimed": int}.
    A fault that is not waiting in the queue (already claimed, or closed)
    is reported as not queued, not claimable, with a count of 0.
    400 for a maintenance task, 404 for an unknown id.
    """
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(status_code=400, detail="Only faults can be claimed.")
    queued = fault in state.faults
    higher = state.faults.higher_priority_count(fault) if queued else 0
    return {"queued": queued, "claimable": queued and higher == 0, "higher_priority_unclaimed": higher}


@router.post("/faults/{incident_id}/claim")
def claim_fault(incident_id: str, state: AppState = Depends(get_state),
                current_user: User = Depends(get_current_user),
                broadcaster: EventBroadcaster = Depends(get_broadcaster),
                client_id: Optional[str] = Depends(get_client_id)):
    """
    Claim one specific fault (from its incident page) - allowed only if no
    strictly more severe fault is still waiting (see
    FaultPriorityQueue.ensure_claimable: 409 with the count of higher-
    priority faults otherwise, or 409 if the fault is not queued at all).
    Unlike /faults/claim-next, which always takes the head of the queue,
    any fault in the top severity group may be claimed.

    Order matters: the priority rule is checked first, then the domain
    claim (status, assignee, log comment), and only then is the fault taken
    out of the queue - so a rejected claim changes nothing anywhere.
    400 for a maintenance task, 404 for an unknown id.
    """
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(status_code=400, detail="Only faults can be claimed.")
    state.faults.ensure_claimable(fault)
    fault.claim(current_user)
    state.faults.remove(fault)
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
    """
    Close a fault - either one still waiting in the priority queue (closed
    WITHOUT ever being claimed) or one already claimed and in progress.
    Any role may close; the admin-only resolution types (Not an Incident /
    By Design) are still enforced by the domain layer.

    The message is the required reason, and Incident.close() adds the log
    comment ("Status changed from Open to Closed. Reason: ...").

    Queue handling: the fault is removed from the priority queue only
    AFTER close() succeeds, so a rejected close (blank message, admin-only
    resolution by a regular user, already closed) leaves a queued fault
    exactly where it was. remove() is a harmless no-op for a fault that was
    already claimed and so is no longer queued.
    """
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(
            status_code=400,
            detail="Maintenance tasks are closed via /incidents/maintenance/{id}/close, not this endpoint.",
        )
    fault.close(current_user, body.resolution_type, body.message)
    state.faults.remove(fault)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "closed", current_user)
    return incident_to_dict(fault)


@router.post("/faults/{incident_id}/reopen")
def reopen_fault(incident_id: str, body: ReopenFaultRequest, state: AppState = Depends(get_state),
                 current_user: User = Depends(get_current_user),
                 broadcaster: EventBroadcaster = Depends(get_broadcaster),
                 client_id: Optional[str] = Depends(get_client_id)):
    """
    Reopen a closed fault as Open or In progress. Admin-only (enforced by
    the domain layer: 403 for a regular user), reason required (400 if
    blank), 409 if the fault is not currently closed, 400 if the target
    status is "closed", 400 if the id belongs to a maintenance task.

    Domain side (Fault.reopen): clears the resolution, logs a comment with
    the reason and the previous resolution, and sets the assignee - the
    admin for In progress, nobody for Open.

    Queue side (this function): an Open fault re-enters the priority queue
    via push(), which puts it at the BACK of its own severity group (the
    queue's arrival counter is fresh). An In-progress fault is not queued -
    it is being worked on right now. remove() runs first purely as a
    safeguard so a fault can never end up in the heap twice.
    """
    fault = state.incidents.get(incident_id)
    if not isinstance(fault, Fault):
        raise HTTPException(status_code=400, detail="Only faults can be reopened via this endpoint.")
    fault.reopen(current_user, body.status, body.reason)
    state.faults.remove(fault)
    if fault.status == IncidentStatus.OPEN:
        state.faults.push(fault)
    state.incidents.save(fault)
    _publish(broadcaster, client_id, fault, "reopened", current_user)
    return incident_to_dict(fault)
