"""
The re-analysis rule: when may a fault be analyzed AGAIN?

A fault that already had a SUCCESSFUL analysis cannot be analyzed again
while nothing has changed - same provider, same data - because the answer
would be the same and the request would only use up quota. It can be
analyzed again as soon as:
  - someone changed the fault since that analysis: a new comment or update,
    a status change (claimed, closed/reopened), a new severity, a new
    assignee, or an edited title, description or details; or
  - a DIFFERENT AI provider is configured now (e.g. Gemini then, Claude now).

An admin can still FORCE it with the same provider and unchanged data
("Re-analyze"), but only once the cooldown has passed since the last
successful analysis (AI_REANALYSIS_COOLDOWN_SECONDS, default 300 = 5 minutes).

Failed analyses (an error comment) and cancelled ones do not count: only a
posted answer is remembered (SqliteAnalysisJobStore.record_success).

"Changed" is decided with a fingerprint: a hash of exactly the fault data
the model is given (app/ai/prompt.py), WITHOUT the AI's own answers and
without the "... requested ... analysis." bookkeeping comments - so
requesting, cancelling or receiving an analysis never counts as a change by
itself. A short hash is stored, never the data.

Usage (app/ai/worker.py):
    fingerprint = analysis_fingerprint(fault, AI_USERNAME)
    decision = decide(store.last_success(fault.id), fingerprint, provider.display_name,
                      is_admin, cooldown_seconds)
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from app.models import Fault

DEFAULT_COOLDOWN_SECONDS = 300.0

# The request/cancel comments the worker writes (see request_comment / cancel_comment
# in app/ai/worker.py), e.g. "tech1 requested Gemini analysis." or
# "admin requested Gemini analysis again (forced)." - not changes to the fault.
_BOOKKEEPING = re.compile(r"^\S+ requested (?:to cancel )?.+ analysis(?: again \(forced\))?\.$")


class LastAnalysis(NamedTuple):
    """The last successful analysis of one fault (stored in SQLite)."""
    provider: str            # display name, e.g. "Gemini"
    model: str               # the model that answered
    fingerprint: str         # analysis_fingerprint() of the data it was given
    finished_at: datetime    # when the answer was posted (UTC)


class Decision(NamedTuple):
    """What decide() says about a new request for one fault."""
    allowed: bool                 # a normal request is allowed
    reason: Optional[str]         # why not (shown to the user), None when allowed
    can_force: bool               # an admin may force it right now
    force_wait_seconds: int       # > 0: seconds until an admin may force it


ALLOWED = Decision(True, None, False, 0)


def analysis_fingerprint(fault: Fault, ai_username: str) -> str:
    """A short hash of the fault data an analysis is based on (see the module docstring)."""
    data = {
        "title": fault.title,
        "description": fault.description,
        "status": fault.status.value,
        "severity": fault.severity.value,
        "details": fault.details,
        "assigned_to": fault.assigned_to.username if fault.assigned_to else None,
        "comments": [[c.author.username, c.text] for c in list(fault.comments)
                     if c.author.username != ai_username and not _BOOKKEEPING.match(c.text)],
    }
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def decide(last: Optional[LastAnalysis], fingerprint: str, provider: str, is_admin: bool,
           cooldown_seconds: float, now: Optional[datetime] = None) -> Decision:
    """
    May the fault be analyzed now? `last` is its last successful analysis (or
    None), `fingerprint` its data now, `provider` the provider configured now.
    """
    if last is None or last.provider != provider or last.fingerprint != fingerprint:
        return ALLOWED
    now = now or datetime.now(timezone.utc)
    elapsed = (now - last.finished_at).total_seconds()
    reason = (f"{last.provider} already analyzed this fault {_ago(elapsed)} and nothing has changed since. "
              f"Add an update first" + ("." if is_admin else ", or ask an admin to run it again."))
    if not is_admin:
        return Decision(False, reason, False, 0)
    wait = max(0, int(round(cooldown_seconds - elapsed)))
    return Decision(False, reason, wait == 0, wait)


def _ago(seconds: float) -> str:
    """'just now', '4 minutes ago', '2 hours ago', '3 days ago'."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def wait_text(seconds: int) -> str:
    """A cooldown wait as 'm:ss', e.g. 192 -> '3:12'."""
    return f"{seconds // 60}:{seconds % 60:02d}"
