"""
Site health report - the decision-support tool of the Sites & Mailing
Lists subsystem (group-of-four extension, requirement 7).

It answers "which sites need attention, and is anyone on it?" by combining
three sources: the sites and mailing lists (SiteDirectory), the incidents
that mention each site (the incident scan, app/site_scan.py), and the
notifications. build_site_report() produces a SiteReport with:

    rows                 one row per active site, most urgent first
    unreported_outages   sites Down/Degraded that NO active incident mentions
                         (an outage nobody is working on)
    unflagged_incidents  sites still Operational although active incidents
                         mention them (status may be out of date)
    uncovered_sites      active sites no active mailing list covers (nobody
                         would be notified about them)
    lists_without_sites / lists_without_members   active lists that cannot
                         notify anyone about anything
    status_counts, notification_counts            totals by status / state
    archived_references, unknown_references       incidents pointing at
                         archived or non-existent sites

Urgency order (attention_key): Down, Degraded, Unknown, Maintenance,
Operational; within the same status, more active incidents first, then by
site id.

The module is plain collection processing (dicts, sets, comprehensions,
sorting) on top of the domain objects; it never changes anything.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from app.models import Incident, NotificationState, SiteStatus
from app.site_scan import SiteScanResult
from app.sites import SiteDirectory

# Lower number = more urgent. Used by attention_key() below.
_URGENCY = {
    SiteStatus.DOWN: 0,
    SiteStatus.DEGRADED: 1,
    SiteStatus.UNKNOWN: 2,
    SiteStatus.MAINTENANCE: 3,
    SiteStatus.OPERATIONAL: 4,
}
_PROBLEM_STATUSES = {SiteStatus.DOWN, SiteStatus.DEGRADED}


@dataclass(frozen=True)
class SiteReportRow:
    """One active site's line in the report - a fixed record, so the dataclass is frozen (immutable)."""
    site_id: int
    site_name: str
    status: SiteStatus
    incident_ids: Tuple[str, ...]
    list_ids: Tuple[str, ...]
    last_checked_at: Optional[datetime]

    @property
    def incident_count(self) -> int:
        return len(self.incident_ids)

    def to_dict(self) -> dict:
        return {"site_id": self.site_id, "site_name": self.site_name, "status": self.status.value,
                "incident_ids": list(self.incident_ids), "incident_count": self.incident_count,
                "list_ids": list(self.list_ids),
                "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None}


def attention_key(row: SiteReportRow) -> Tuple[int, int, int]:
    """
    Sort key for "most urgent first": (status urgency, minus incident count,
    site id). A regular named function rather than a lambda because the rule
    has three parts and deserves a name and a docstring.
    """
    return _URGENCY[row.status], -row.incident_count, row.site_id


@dataclass
class SiteReport:
    """The finished report. See the module docstring for what each field means."""
    rows: List[SiteReportRow]
    unreported_outages: List[int]
    unflagged_incidents: List[int]
    uncovered_sites: List[int]
    lists_without_sites: List[str]
    lists_without_members: List[str]
    status_counts: Dict[str, int]
    notification_counts: Dict[str, int]
    archived_references: Dict[int, List[str]] = field(default_factory=dict)
    unknown_references: Dict[int, List[str]] = field(default_factory=dict)

    @property
    def most_urgent(self) -> Optional[SiteReportRow]:
        """The first row, if it needs attention at all (Down or Degraded) - otherwise None."""
        if not self.rows:
            return None
        first, *_rest = self.rows          # star-unpacking: the head of the sorted rows, the rest ignored
        return first if first.status in _PROBLEM_STATUSES else None

    def to_dict(self) -> dict:
        """Plain-dict form for the API (step 7)."""
        return {
            "rows": [row.to_dict() for row in self.rows],
            "unreported_outages": self.unreported_outages,
            "unflagged_incidents": self.unflagged_incidents,
            "uncovered_sites": self.uncovered_sites,
            "lists_without_sites": self.lists_without_sites,
            "lists_without_members": self.lists_without_members,
            "status_counts": self.status_counts,
            "notification_counts": self.notification_counts,
            "archived_references": {str(k): v for k, v in self.archived_references.items()},
            "unknown_references": {str(k): v for k, v in self.unknown_references.items()},
            "most_urgent": self.most_urgent.site_id if self.most_urgent else None,
        }

    def lines(self) -> List[str]:
        """A readable plain-text rendering (used by main.py's demo)."""
        out = ["Site health report", "------------------"]
        for status, count in self.status_counts.items():          # .items() with unpacking
            out.append(f"  {status:<12} {count}")
        urgent = self.most_urgent
        out.append(f"Most urgent: Site {urgent.site_id} ({urgent.site_name}, {urgent.status.value})"
                   if urgent else "Most urgent: nothing needs attention")
        for row in self.rows:
            out.append(f"  Site {row.site_id:<5} {row.status.value:<12} {row.incident_count} active incident(s)  "
                       f"{row.site_name}")
        checks = [("Unreported outages", self.unreported_outages),
                  ("Operational but mentioned by incidents", self.unflagged_incidents),
                  ("Sites no list covers", self.uncovered_sites),
                  ("Lists with no sites", self.lists_without_sites),
                  ("Lists with no members", self.lists_without_members),
                  ("Incidents mentioning archived sites", sorted(self.archived_references)),
                  ("Incidents mentioning unknown sites", sorted(self.unknown_references))]
        for title, items in checks:
            out.append(f"{title}: {', '.join(map(str, items)) if items else 'none'}")
        drafts = self.notification_counts.get(NotificationState.DRAFT.value, 0)
        out.append(f"Notifications waiting for a decision: {drafts}")
        return out

    def __str__(self) -> str:
        return "\n".join(self.lines())


def count_by(values: Iterable[str], keys: Iterable[str]) -> Dict[str, int]:
    """
    Counting dict: how many times each value occurs, with every key in `keys`
    present (0 if it never occurs) and in that order. Built with a dict
    comprehension for the zero-filled start, then a plain counting loop.
    """
    counts = {key: 0 for key in keys}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_site_report(directory: SiteDirectory, incidents: Iterable[Incident]) -> SiteReport:
    """
    Build the site health report from the directory and the incidents
    (normally IncidentRepository.list_all()). Runs the incident scan itself,
    through SiteDirectory.scan_incidents() (ids and full site names).
    """
    sites = list(directory.sites())                                  # active only
    active_lists = list(directory.mailing_lists())
    # The directory's own wrapper matches by id AND by full site name (see app/site_scan.py).
    scan: SiteScanResult = directory.scan_incidents(incidents)

    # Which lists cover which site: a grouping dict (site id -> list ids).
    lists_by_site: Dict[int, List[str]] = {}
    for mailing_list in active_lists:
        for site_id in mailing_list.site_ids:
            lists_by_site.setdefault(site_id, []).append(mailing_list.list_id)

    rows = sorted(
        (SiteReportRow(site_id=s.site_id, site_name=s.site_name, status=s.status,
                       incident_ids=scan.incidents_for(s.site_id),
                       list_ids=tuple(lists_by_site.get(s.site_id, [])),
                       last_checked_at=s.last_check.checked_at if s.last_check else None)
         for s in sites),
        key=attention_key,
    )

    # Set algebra over site ids.
    active_ids: Set[int] = {s.site_id for s in sites}
    problem_ids = {s.site_id for s in sites if s.status in _PROBLEM_STATUSES}
    operational_ids = {s.site_id for s in sites if s.status == SiteStatus.OPERATIONAL}
    covered_ids: Set[int] = set().union(*(ml.site_ids for ml in active_lists))
    with_incidents = scan.sites_with_incidents

    return SiteReport(
        rows=rows,
        unreported_outages=sorted(problem_ids - with_incidents),       # difference
        unflagged_incidents=sorted(operational_ids & with_incidents),  # intersection
        uncovered_sites=sorted(active_ids - covered_ids),              # difference
        lists_without_sites=[ml.list_id for ml in active_lists if not ml.site_ids],   # already in id order
        # ordered by display name (case-insensitive), the way the page lists them
        lists_without_members=[ml.list_id for ml in sorted(active_lists, key=lambda ml: ml.name.lower())
                               if len(ml) == 0],
        status_counts=count_by((s.status.value for s in sites), (status.value for status in _URGENCY)),
        notification_counts=count_by((n.state.value for n in directory.notifications()),
                                     (state.value for state in NotificationState)),
        archived_references={site_id: sorted(ids) for site_id, ids in scan.archived.items()},
        unknown_references={site_id: sorted(ids) for site_id, ids in scan.unknown.items()},
    )
