"""
Iteration and lazy processing over incidents (Stage 1, Part D items 1-3).

Item 1 - Iterable + separate Iterator classes: `FifoQueueIterator` and
         `SeverityOrderIterator` (top of this file), returned by
         MaintenanceQueue.__iter__ and FaultPriorityQueue.__iter__.
Item 2 - Generator with yield: `stale_in_progress_work` (bottom of this file).
Item 3 - Expression generators and a lazy pipeline: `pressing_maintenance_calls`.

--- Item 1: Iterable and Iterator ---

The two queues are the ITERABLES: they own the data and implement
__iter__. Each __iter__ call returns a brand-new ITERATOR object from one of
the classes below. The iterator owns only the traversal state - its own
snapshot of the items and its current position - and implements the
iterator protocol: __next__ returns the next item or raises StopIteration
when there are no more, and __iter__ returns the iterator itself.

Because every iter(queue) builds a separate iterator with its own position,
two iterators over the same queue advance independently - moving one never
moves the other. Each iterator works on a SNAPSHOT taken when it was
created, so tasks added to or removed from the queue meanwhile neither
break the iteration (a deque raises "mutated during iteration" otherwise)
nor appear half-way through it. Iterating never changes the queue itself.

The class names state the traversal rule:
    FifoQueueIterator      pending maintenance tasks in arrival order (FIFO)
    SeverityOrderIterator  queued faults most severe first, ties by arrival -
                           a sorted copy, because a heap's internal list is
                           NOT in priority order (only its first item is
                           guaranteed to be the smallest)

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

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Iterable, Iterator, List, Optional, Tuple

from app.models import Fault, Incident, IncidentStatus, MaintenanceTask


# --- Item 1: Iterator classes ---

class SnapshotIterator(ABC):
    """
    Shared iterator protocol for the queue iterators: holds a list snapshot
    and a position, and hands the items out one by one. Subclasses only
    decide HOW the snapshot is ordered (_ordered()), which is what makes
    each subclass's traversal rule its own.
    """

    def __init__(self, source):
        self._items: list = self._ordered(source)
        self._position = 0

    @staticmethod
    @abstractmethod
    def _ordered(source) -> list:
        """Build this iterator's snapshot from the queue's internal storage, in traversal order."""

    def __iter__(self) -> "SnapshotIterator":
        """An iterator is its own iterable - `for x in it` continues from where it is now."""
        return self

    def __next__(self):
        """Return the next item, or raise StopIteration once every item has been returned (and on every call after)."""
        if self._position >= len(self._items):
            raise StopIteration
        item = self._items[self._position]
        self._position += 1
        return item

    @property
    def remaining(self) -> int:
        """How many items this iterator has not returned yet."""
        return len(self._items) - self._position

    def __str__(self) -> str:
        return f"{self.__class__.__name__}(item {self._position} of {len(self._items)})"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(position={self._position}, size={len(self._items)})"


class FifoQueueIterator(SnapshotIterator):
    """
    Iterator over a MaintenanceQueue's PENDING tasks in arrival order
    (first in, first out) - the order they will be started. The task
    currently in progress is not included, matching what the queue's
    "pending" list has always meant. Returned by MaintenanceQueue.__iter__.
    """

    @staticmethod
    def _ordered(source: Iterable[MaintenanceTask]) -> List[MaintenanceTask]:
        return list(source)   # the deque is already in arrival order - just copy it


class SeverityOrderIterator(SnapshotIterator):
    """
    Iterator over a FaultPriorityQueue's faults, most severe first and, for
    equal severity, in arrival order - the order they will be claimed.
    Nothing is popped. Returned by FaultPriorityQueue.__iter__.
    """

    @staticmethod
    def _ordered(source: Iterable[Tuple[int, int, Fault]]) -> List[Fault]:
        # Heap entries are (severity value, arrival counter, fault); sorting
        # the tuples orders by severity, then arrival - the claiming order.
        return [fault for _, _, fault in sorted(source)]

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
    open_calls = (i for i in source if i.kind == MaintenanceTask.KIND and i.status == IncidentStatus.OPEN)
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
