"""
Incident scan: which active incidents mention which org sites (Sites &
Mailing Lists subsystem - this is the link between the new subsystem and
the core incident model).

An incident mentions a site when its title, description or any comment
contains either:

1. THE SITE'S ID after the word "site" (ID_PATTERN), case-insensitive.
   Between the word and the id there may be whitespace, a "#", or a "-"
   (with optional spaces around the "#" / "-"):
       "Site 1042", "site 1042", "Site #1042", "Site#1042", "Site-1042",
       "Site - 1042", "SITE   1042"
   The word must be "site" itself ("Website 1042" and "Sites 1042" do not
   count), and the id must end at a word boundary, so "Site 10420" is site
   10420, never site 1042. Other separators ("Site: 1042", "Site1042") do
   not count.

2. THE SITE'S FULL NAME (NameMatcher), e.g. "Internal HR portal is down"
   or "Error loading `Internal HR portal`". Only the COMPLETE name counts:
   "HR portal" does not match a site named "Internal HR portal". The name
   must stand on its own (not glued to other letters or digits: "Internal
   HR portals" does not match), and its characters and spacing must be
   exactly the site's name - only letter case is ignored, see
   NAME_MATCH_IGNORES_CASE. When one name contains another (sites "Portal"
   and "Customer portal"), the longest name wins where they overlap, so
   "Customer portal is down" counts for "Customer portal" only.
   Names are not unique: if two sites share a name, a mention of it counts
   for both.

The scan is a LAZY PIPELINE of generators - nothing is read until a consumer
asks for the next mention, and no intermediate lists are built:

    stage 1  _active(incidents)       keep only OPEN / IN_PROGRESS incidents
    stage 2  _texts(incident)         every text field of one incident, tagged
                                      with where it came from
    stage 3  iter_site_mentions(...)  every id or name match, as a SiteMention

scan_incidents() then consumes the whole pipeline once and groups the
mentions by site, splitting them into three buckets by what the site id
refers to: an active site, an archived site, or no site at all (an unknown
id - the cross-reference check between incident data and site data; names
can only ever match known sites, so only id mentions can be unknown).

The scan reads incidents from wherever it is given them (normally
IncidentRepository.list_all(), the in-memory mirror of the database), and
never changes anything. Because names are matched at scan time, renaming a
site changes which incidents mention it from the next scan on.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Set, Tuple

from app.models import Incident, IncidentStatus

# "site" + (whitespace | optional spaces, "#" or "-", optional spaces) + digits,
# as whole words, any letter case.
ID_PATTERN = re.compile(r"\bsite(?:\s+|\s*[#-]\s*)(\d+)\b", re.IGNORECASE)

# Site names are matched ignoring letter case ("internal hr portal" counts for
# "Internal HR portal"). Set to False to require the exact letter case too.
NAME_MATCH_IGNORES_CASE = True

_ACTIVE_STATUSES = {IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS}


class SiteMention(NamedTuple):
    """
    One match: which site, in which incident, where ('title', 'description'
    or 'comment'), and how it was recognised ('id' or 'name').
    """
    site_id: int
    incident_id: str
    found_in: str
    matched_by: str = "id"


class NameMatcher:
    """
    Finds full site names in text (rule 2 in the module docstring).

    All names are compiled into ONE regular expression of alternatives,
    longest name first, each wrapped in "not preceded / followed by a letter
    or digit" checks. finditer() scans left to right and, at each position,
    takes the first alternative that matches - the longest name - and never
    re-reads the text it consumed. That is what makes the longest name win
    where names overlap.
    """

    def __init__(self, names: Mapping[int, str]):
        flags = re.IGNORECASE if NAME_MATCH_IGNORES_CASE else 0
        self._ids_by_key: Dict[str, List[int]] = {}
        for site_id, name in names.items():
            if name and name.strip():
                self._ids_by_key.setdefault(self._key(name.strip()), []).append(site_id)
        alternatives = sorted({name.strip() for name in names.values() if name and name.strip()},
                              key=len, reverse=True)
        self._pattern: Optional[re.Pattern] = (
            re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in alternatives) + r")(?!\w)", flags)
            if alternatives else None
        )

    @staticmethod
    def _key(text: str) -> str:
        return text.lower() if NAME_MATCH_IGNORES_CASE else text

    def find(self, text: str) -> Iterator[int]:
        """Site ids whose full name appears in `text` (a site may be yielded more than once)."""
        if self._pattern is None:
            return
        for match in self._pattern.finditer(text):
            yield from self._ids_by_key.get(self._key(match.group(0)), ())


def _active(incidents: Iterable[Incident]) -> Iterator[Incident]:
    """Stage 1: only incidents still being dealt with (open or in progress)."""
    return (incident for incident in incidents if incident.status in _ACTIVE_STATUSES)


def _texts(incident: Incident) -> Iterator[Tuple[str, str]]:
    """Stage 2: (where, text) for every text field of one incident - title, description, then each comment."""
    yield "title", incident.title
    yield "description", incident.description
    for comment in incident.comments:
        yield "comment", comment.text


def _mentions_in(text: str, names: Optional[NameMatcher]) -> Iterator[Tuple[int, str]]:
    """(site_id, 'id' / 'name') for every mention in one piece of text - ids first, then names."""
    for match in ID_PATTERN.finditer(text):
        yield int(match.group(1)), "id"
    if names is not None:
        for site_id in names.find(text):
            yield site_id, "name"


def iter_site_mentions(incidents: Iterable[Incident],
                       site_names: Optional[Mapping[int, str]] = None) -> Iterator[SiteMention]:
    """
    Stage 3: every site mention in every active incident, lazily, in
    incident order. `site_names` ({site_id: name}) enables name matching;
    without it only ids are matched. The same site may be yielded several
    times for one incident (e.g. by id in the title and by name in a
    comment) - grouping is the caller's job (see scan_incidents()).
    """
    names = NameMatcher(site_names) if site_names else None
    return (SiteMention(site_id, incident.id, found_in, matched_by)
            for incident in _active(incidents)
            for found_in, text in _texts(incident)
            for site_id, matched_by in _mentions_in(text, names))


@dataclass
class SiteScanResult:
    """
    Mentions grouped by site id -> set of incident ids (a set, so an incident
    that mentions the same site three times - by id or by name - counts
    once). Three buckets:

        by_site      ids of ACTIVE sites
        archived     ids of archived sites (the incident points at a site that
                     is no longer monitored)
        unknown      ids that match no site at all (likely a typo, or a site
                     that was never imported)
    """
    by_site: Dict[int, Set[str]] = field(default_factory=dict)
    archived: Dict[int, Set[str]] = field(default_factory=dict)
    unknown: Dict[int, Set[str]] = field(default_factory=dict)

    def incidents_for(self, site_id: int) -> Tuple[str, ...]:
        """Ids of the active incidents mentioning an active site, sorted (empty if none)."""
        return tuple(sorted(self.by_site.get(site_id, ())))

    @property
    def sites_with_incidents(self) -> Set[int]:
        """Ids of the active sites that at least one active incident mentions."""
        return set(self.by_site)

    def __str__(self) -> str:
        return (f"SiteScanResult({len(self.by_site)} sites with incidents, "
                f"{len(self.archived)} archived referenced, {len(self.unknown)} unknown referenced)")


def scan_incidents(incidents: Iterable[Incident], active_site_ids: Iterable[int],
                   archived_site_ids: Iterable[int] = (),
                   site_names: Optional[Mapping[int, str]] = None) -> SiteScanResult:
    """
    Run the whole pipeline once and group the mentions. The site ids (and,
    for name matching, {site_id: name} for every known site - active and
    archived) are passed in rather than a SiteDirectory, so this module stays
    independent of how sites are stored. SiteDirectory.scan_incidents()
    (app/sites.py) is the normal caller and passes all three.
    """
    active, archived = set(active_site_ids), set(archived_site_ids)
    result = SiteScanResult()
    for mention in iter_site_mentions(incidents, site_names):
        if mention.site_id in active:
            bucket = result.by_site
        elif mention.site_id in archived:
            bucket = result.archived
        else:
            bucket = result.unknown
        bucket.setdefault(mention.site_id, set()).add(mention.incident_id)
    return result
