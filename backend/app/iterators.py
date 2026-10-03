"""
Lazy processing over incidents (Stage 1, Part D items 2 and 3).

Item 2 - Generator with yield: `stale_in_progress_work` (bottom of this file).
Item 3 - Expression generators and a lazy pipeline: `pressing_maintenance_calls`.

--- Item 3: lazy pipeline ---

The pipeline answers a real operational question: "which maintenance calls
have been waiting too long and are not being worked on?" It is built from
three chained generator expressions - no list is ever created between the
stages:

    stage 1: keep only OPEN maintenance calls          (criterion 1: calls already
                                                        in progress - or closed - are left out)
    stage 2: keep only calls open for more than        (criterion 2: time since opening)
             3 days, converting each to a tuple
    stage 3: turn each tuple into {id, title, created_by, created_at, days_open, summary}

Nothing runs when the pipeline is built. Work only happens when a consumer
asks for the next value (next(), a for loop, islice, ...). See the README
section "Lazy pipeline" for the full explanation.
"""

from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Iterable, Iterator, List, Optional

from app.models import Incident, IncidentStatus, MaintenanceTask

# A maintenance call open for MORE than this long counts as "pressing".
PRESSING_AFTER = timedelta(days=3)

# In-progress work with no update for MORE than this long counts as "stale".
STALE_AFTER = timedelta(hours=4)


class ExaminedCounter:
    """
    Tiny helper that counts how many incidents the pipeline actually pulled
    from its source. It exists only to make laziness visible (in the tests
    and the API response): after consuming a few results, the count shows
    how many incidents never had to be looked at.
    """

    def __init__(self) -> None:
        self.count = 0


def _counting(source: Iterable[Incident], counter: ExaminedCounter) -> Iterator[Incident]:
    """Generator that passes items through unchanged and counts each one pulled."""
    for item in source:
        counter.count += 1
        yield item


def pressing_maintenance_calls(incidents: Iterable[Incident],
                               counter: Optional[ExaminedCounter] = None,
                               now: Optional[datetime] = None,
                               min_age: timedelta = PRESSING_AFTER) -> Iterator[dict]:
    """
    Build (not run) the three-stage lazy pipeline and return its generator.

    `incidents` may be any iterable (e.g. IncidentRepository.list_all(),
    which yields incidents in creation order, oldest first). `now` is
    injectable so tests don't depend on the real clock. A generator can be
    consumed only once - call this function again to start over.
    """
    now = now or datetime.now(timezone.utc)
    source = _counting(incidents, counter) if counter is not None else incidents

    # Stage 1: maintenance calls that are still OPEN (so not in progress, not closed).
    open_calls = (i for i in source if isinstance(i, MaintenanceTask) and i.status == IncidentStatus.OPEN)
    # Stage 2: open for more than `min_age`, converted to a short tuple.
    overdue = ((t.id, t.title, t.created_by.username, t.created_at, now - t.created_at)
               for t in open_calls if now - t.created_at > min_age)
    # Stage 3: the record returned to the caller (summary field + id).
    return ({"id": incident_id, "title": title, "created_by": created_by,
             "created_at": created_at.isoformat(), "days_open": age.days,
             "summary": f"{title} - open for {age.days} days ({incident_id[:8]})"}
            for incident_id, title, created_by, created_at, age in overdue)


def first_pressing_calls(incidents: Iterable[Incident], limit: Optional[int] = None,
                         now: Optional[datetime] = None) -> tuple:
    """
    Consume only the first `limit` results and stop (every result if
    `limit` is None). Returns (calls, examined) where `examined` is how
    many incidents were pulled from the source to produce them - anything
    after that point was never touched.
    """
    counter = ExaminedCounter()
    calls: List[dict] = list(islice(pressing_maintenance_calls(incidents, counter, now), limit))
    return calls, counter.count


# --- Item 2: generator function with yield ---

def stale_in_progress_work(incidents: Iterable[Incident], now: Optional[datetime] = None,
                           stale_after: timedelta = STALE_AFTER) -> Iterator[Incident]:
    """
    Generator function (it contains `yield`) that hands back, one at a time,
    only the incidents someone has started working on but that have had no
    update (status change or comment) for more than `stale_after` - i.e.
    work that may be stuck. Applies to both faults and maintenance tasks.

    Business condition (both must hold):
      - status is IN_PROGRESS (open and closed incidents are never yielded);
      - `updated_at` is more than `stale_after` in the past.

    Calling this function does NOT run its body: it only creates a generator
    object. Nothing is read from `incidents` until a consumer asks for a
    value (next(), a for loop, ...). After each `yield` the function is
    frozen exactly where it is, local variables included, and resumes from
    that same spot on the next request - so a for loop that starts after one
    next() call carries on with the following incident, not the first one.
    Once it has finished, the generator is exhausted: iterating it again
    gives nothing, and the only way to go through the data again is to call
    this function again to create a new generator.

    `incidents` may be any iterable (e.g. IncidentRepository.list_all()).
    `now` is injectable so tests don't depend on the real clock.
    """
    now = now or datetime.now(timezone.utc)
    for incident in incidents:
        if incident.status != IncidentStatus.IN_PROGRESS:
            continue
        if now - incident.updated_at > stale_after:
            yield incident
