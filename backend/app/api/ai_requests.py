"""
The site portal's AI requests view (frontend/sites.html, "AI requests" tab
and the Archive tab): every AI analysis request from the permanent request
history (app/ai/request_history.py), admin only.

    GET /analysis-requests                 live requests, newest first
    GET /analysis-requests?archived=true   archived ones (view only)

A request is archived once AI_REQUEST_ARCHIVE_DAYS have passed since it
finished (app/archive_rules.py, state.request_archive_after); one that has
not finished - queued or running - never is. Each request comes with its
fault's title, its state ("queued" | "running" | its outcome) and when it
moves (or moved) to the archive. The history is read even while the AI
feature is off, so earlier requests stay visible.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request

from app.ai.request_history import SqliteRequestHistoryStore
from app.ai.worker import AnalysisWorker
from app.api.deps import get_analysis_worker, get_current_admin, get_state
from app.archive_rules import is_request_archived
from app.models import User
from app.state import AppState, database_path

router = APIRouter(prefix="/analysis-requests", tags=["ai-analysis"])


def _history(request: Request, worker: Optional[AnalysisWorker]) -> SqliteRequestHistoryStore:
    """The worker's history store, or - with the AI feature off - one opened once for this view."""
    if worker is not None:
        return worker.history
    store = getattr(request.app.state, "request_history", None)
    if store is None:
        store = SqliteRequestHistoryStore(database_path())
        request.app.state.request_history = store
    return store


@router.get("")
def list_requests(request: Request, archived: bool = False, state: AppState = Depends(get_state),
                  _admin: User = Depends(get_current_admin),
                  worker: Optional[AnalysisWorker] = Depends(get_analysis_worker)) -> dict:
    now = datetime.now(timezone.utc)
    after = state.request_archive_after
    snapshot = worker.queue_snapshot() if worker else {"running": None, "queued": []}
    requests = []
    for row in _history(request, worker).rows():
        finished = datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        if is_request_archived(finished, now, after) != archived:
            continue
        if row["outcome"] is None:
            row["state"] = "running" if row["incident_id"] == snapshot["running"] else "queued"
        else:
            row["state"] = row["outcome"]
        try:
            row["incident_title"] = state.incidents.get(row["incident_id"]).title
        except KeyError:                       # an incident that no longer exists
            row["incident_title"] = None
        row["archive_at"] = (finished + after).isoformat() if finished and after else None
        requests.append(row)
    return {"enabled": worker is not None,
            "archive_days": after.total_seconds() / 86400 if after else None,
            "requests": requests}
