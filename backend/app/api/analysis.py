"""
AI analysis endpoints (one fault at a time). The queue and the worker
thread live in app/ai/worker.py; this router only handles HTTP, the
request/cancel comments, and their live-update events.

    GET  /incidents/faults/{id}/analysis              state of this fault's analysis
    POST /incidents/faults/{id}/analysis              request one     (any logged-in user)
    POST /incidents/faults/{id}/analysis?force=true   Re-analyze with no changes (admin, after the cooldown)
    POST /incidents/faults/{id}/analysis/cancel       cancel it       (any logged-in user)

Every response has the same shape:
    {"enabled": true, "provider": "Gemini", "model": "...", "models": ["...", "..."],
     "state": "idle" | "queued" | "running", "position": 2 | null}
("model" is the preferred one; "models" the whole fallback list, best first.
Both come from the settings until the first analysis creates the provider -
see app/ai/worker.py - so this works before the worker thread starts.)
plus the re-analysis rule for the user asking (app/ai/reanalysis.py):
    "last_analysis": {"provider", "model", "at"} | null, "blocked": bool,
    "reason": str | null, "can_force": bool, "force_wait_seconds": int
With the feature off (the selected provider has no API key - see
app/ai/config.py) GET returns {"enabled": false, ...} and the page hides the
buttons; the POSTs answer 503.

Requesting and cancelling each add a comment by the user ("tech1 requested
Gemini analysis." - written by the worker, see AnalysisWorker.request()),
then this router saves the fault and publishes a normal incident event, so other people
viewing the fault get the usual "this incident changed" prompt - the same as
for any comment. The worker separately publishes the queue event that keeps
every page's buttons up to date.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.ai.worker import AnalysisWorker
from app.api.deps import get_analysis_worker, get_broadcaster, get_client_id, get_current_user, get_state
from app.events import EventBroadcaster, incident_event
from app.models import Fault, User
from app.state import AppState

router = APIRouter(prefix="/incidents/faults", tags=["ai-analysis"])


def _status(worker: Optional[AnalysisWorker], fault: Fault, user: User) -> dict:
    if worker is None:
        return {"enabled": False, "provider": None, "model": None, "models": [], "state": "idle", "position": None,
                "last_analysis": None, "blocked": False, "reason": None, "can_force": False, "force_wait_seconds": 0}
    # Name and models come from the settings until the worker thread has created the provider
    # (v0.4.5 - nothing here imports an SDK); after that, the live (possibly refreshed) list.
    models = worker.models
    return {"enabled": True, "provider": worker.provider_name, "model": models[0],
            "models": models, **worker.status_of(fault.id),
            **worker.reanalysis_status(fault, user)}


def _fault(state: AppState, incident_id: str) -> Fault:
    incident = state.incidents.get(incident_id)          # KeyError -> 404
    if not isinstance(incident, Fault):
        raise HTTPException(status_code=400, detail="AI analysis is only available for faults.")
    return incident


def _require(worker: Optional[AnalysisWorker]) -> AnalysisWorker:
    if worker is None:
        raise HTTPException(status_code=503, detail="AI analysis is not configured on this server "
                                                    "(see AI_PROVIDER in backend/.env).")
    return worker


@router.get("/{incident_id}/analysis")
def get_analysis_status(incident_id: str, state: AppState = Depends(get_state),
                        worker: Optional[AnalysisWorker] = Depends(get_analysis_worker),
                        current_user: User = Depends(get_current_user)):
    return _status(worker, _fault(state, incident_id), current_user)


@router.post("/{incident_id}/analysis")
def request_analysis(incident_id: str, force: bool = Query(False), state: AppState = Depends(get_state),
                     worker: Optional[AnalysisWorker] = Depends(get_analysis_worker),
                     current_user: User = Depends(get_current_user),
                     broadcaster: EventBroadcaster = Depends(get_broadcaster),
                     client_id: Optional[str] = Depends(get_client_id)):
    """
    Queue an analysis. 409 if the fault is closed, already pending, already
    analyzed with no changes since (see app/ai/reanalysis.py), or the queue is
    full. `force=true` (admins, after the cooldown) runs it again anyway; 403
    for a non-admin.
    """
    worker = _require(worker)
    fault = _fault(state, incident_id)
    worker.request(fault, current_user, force)  # also adds the "requested" comment
    state.incidents.save(fault)
    broadcaster.publish(incident_event(fault, "ai_analysis_requested", current_user.username),
                        exclude_client_id=client_id)
    return _status(worker, fault, current_user)


@router.post("/{incident_id}/analysis/cancel")
def cancel_analysis(incident_id: str, state: AppState = Depends(get_state),
                    worker: Optional[AnalysisWorker] = Depends(get_analysis_worker),
                    current_user: User = Depends(get_current_user),
                    broadcaster: EventBroadcaster = Depends(get_broadcaster),
                    client_id: Optional[str] = Depends(get_client_id)):
    """Cancel this fault's pending analysis, queued or running (409 if there is none)."""
    worker = _require(worker)
    fault = _fault(state, incident_id)
    worker.cancel(fault, current_user)         # also adds the "requested to cancel" comment
    state.incidents.save(fault)
    broadcaster.publish(incident_event(fault, "ai_analysis_cancelled", current_user.username),
                        exclude_client_id=client_id)
    return _status(worker, fault, current_user)
