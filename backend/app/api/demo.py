"""
The requirements demo page (frontend/demo.html): every Stage 1 requirement
with its summary, the code that meets it, where the app uses it, and a demo
that runs on fresh in-memory data (app/requirements_demo.py).

    GET  /demo/requirements             the list (for any logged-in user)
    POST /demo/requirements/{id}/run    run that requirement's demo -> its output lines

Only the demos listed in app/requirements_demo.py can run - nothing sent by
the browser is executed - and they never touch the app's real data.
"""

from fastapi import APIRouter, Depends

from app import requirements_demo
from app.api.deps import get_current_user
from app.models import User

router = APIRouter(prefix="/demo", tags=["demo"])


@router.get("/requirements")
def list_requirements(user: User = Depends(get_current_user)):
    return {"stage": 1, "requirements": list(requirements_demo.describe_all()),
            "note": "Stage 2 requirements are added after the team's Stage 2 work is merged."}


@router.post("/requirements/{requirement_id}/run")
def run_requirement(requirement_id: str, user: User = Depends(get_current_user)):
    return requirements_demo.run(requirement_id)          # KeyError (unknown id) -> 404
