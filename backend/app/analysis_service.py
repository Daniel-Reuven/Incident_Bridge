"""
AnalysisListener (the Observer pattern) - lets code outside the AI worker
react to a finished analysis without the worker (app/ai/worker.py) knowing
anything about them.

AnalysisWorker takes a list of listeners (a constructor argument, empty by
default - every existing caller and test keeps working unchanged) and,
after an answer is posted WITH valid structured findings (app/ai/result.py
- outcome success, never success_unstructured), calls
on_analysis_completed() on each one with one AnalysisCompleted. A listener
that raises is logged and never stops the other listeners, or the comments
already posted - the AI service's own job is done the moment the answer is
posted. Failure of the AI service itself (timeout, HTTP error, invalid
reply) never reaches here at all: no event, no site change, just today's
error comment.

Two listeners ship here:

- SiteImpactListener: for every affected_site_id the findings name, runs a
  REAL availability check (SiteDirectory.check_site(), app/sites.py) if the
  site exists, is active and is not in Maintenance. The check (not the AI)
  decides the site's status, and the existing rules draft any notification
  - an admin still has to send it. An id that does not exist, or belongs to
  an archived site, is an invalid cross-reference: it is logged and nothing
  about that site changes, the same stance app/sites.py already takes
  towards a mailing list's bad site links. Each check is published like a
  check from the site portal ("site_checked", by ai-assistant), so it is
  in the activity log and open portal pages update at once.
- AnalysisStatsListener: counts, per site, how many completed analyses
  named it - for a report.
"""

import threading
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Protocol

from app.ai.log import log
from app.ai.result import AnalysisFindings
from app.events import EventBroadcaster
from app.models import Fault
from app.models.enums import SiteStatus
from app.models.system_users import AI_USERNAME
from app.sites import SiteDirectory


@dataclass(frozen=True)
class AnalysisCompleted:
    incident: Fault
    findings: AnalysisFindings
    provider: str
    model: str


class AnalysisListener(Protocol):
    def on_analysis_completed(self, event: AnalysisCompleted) -> None:
        ...


def notify_listeners(listeners: Iterable[AnalysisListener], event: AnalysisCompleted) -> None:
    """Call every listener in order; one that raises is logged and never stops the others (app/ai/worker.py)."""
    for listener in listeners:
        try:
            listener.on_analysis_completed(event)
        except Exception as exc:
            log.error(f"Analysis listener {listener!r} failed on fault {event.incident.id} "
                     f"({type(exc).__name__}: {exc})")


class SiteImpactListener:
    """Runs a real availability check on every affected site the AI findings name - see the module docstring."""

    def __init__(self, directory: SiteDirectory, broadcaster: Optional[EventBroadcaster] = None):
        self._directory = directory
        self._broadcaster = broadcaster      # the app's: each check is published like one from the portal

    def on_analysis_completed(self, event: AnalysisCompleted) -> None:
        for site_id in event.findings.affected_site_ids:
            try:
                site = self._directory.get_site(site_id)
            except KeyError:
                log.info(f"Fault {event.incident.id}: AI findings named site {site_id}, which does not "
                         f"exist - invalid cross-reference, ignored")
                continue
            if site.is_archived:
                log.info(f"Fault {event.incident.id}: AI findings named site {site_id}, which is "
                         f"archived - invalid cross-reference, ignored")
                continue
            if site.status == SiteStatus.MAINTENANCE:
                log.info(f"Fault {event.incident.id}: AI findings named {site.label}, which is in "
                         f"Maintenance - not checked")
                continue
            try:
                outcome = self._directory.check_site(site_id)
            except RuntimeError as exc:        # already being checked, or no checker configured
                log.warning(f"Fault {event.incident.id}: could not check {site.label} ({exc})")
                continue
            log.info(f"Fault {event.incident.id}: AI findings named {site.label} - checked, "
                     f"{outcome.check.summary}")
            self._publish(site_id, outcome, event)

    def _publish(self, site_id: int, outcome, event: AnalysisCompleted) -> None:
        """The same "site_checked" event as a check from the portal (app/api/sites.py), by ai-assistant."""
        if self._broadcaster is None:
            return
        site = self._directory.get_site(site_id)
        self._broadcaster.publish(
            {"scope": "sites", "action": "site_checked", "actor": AI_USERNAME,
             "subject": f"{site.label} ({site.site_name})", "site_id": site_id, "status": site.status.value,
             "changed": outcome.change is not None,
             "notification": outcome.notification.state.value if outcome.notification else None,
             "reason": f"named by the AI analysis of fault {event.incident.id}"},
            roles={"admin"})


class AnalysisStatsListener:
    """Counts completed analyses per site (from the AI's affected_site_ids) - for a report."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Dict[int, int] = {}

    def on_analysis_completed(self, event: AnalysisCompleted) -> None:
        with self._lock:
            for site_id in event.findings.affected_site_ids:
                self._counts[site_id] = self._counts.get(site_id, 0) + 1

    def counts(self) -> Dict[int, int]:
        """{site_id: how many completed analyses named it} - a snapshot safe to read from any thread."""
        with self._lock:
            return dict(self._counts)

    def __repr__(self) -> str:
        return f"AnalysisStatsListener(sites={len(self._counts)})"


__all__ = ["AnalysisCompleted", "AnalysisListener", "notify_listeners", "SiteImpactListener", "AnalysisStatsListener"]
