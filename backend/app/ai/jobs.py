"""
AnalysisJob: one request to analyze one fault, waiting in (or running from)
the AI analysis queue - see app/ai/worker.py.

A job holds only ids and names, never the fault's text: the prompt is built
from the fault as it is when the job STARTS (a confirmed design decision),
so comments added while it waited are included.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class AnalysisJob:
    incident_id: str
    requested_by: str                     # username of whoever pressed the button
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Set when someone cancels the job WHILE it is running: the API call is
    # left to finish (a running HTTP call cannot be cleanly interrupted from
    # another thread), and its answer is then thrown away instead of posted.
    cancelled: bool = False
