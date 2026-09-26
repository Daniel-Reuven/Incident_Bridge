"""Single shared fault priority queue, all severities (README sections 2, 3, 8)."""

import heapq
import itertools
from typing import Iterator, List, Tuple

from app.models.fault import Fault


class FaultPriorityQueue:
    """
    Single shared priority queue for Fault incidents of every severity -
    Critical, Major, and Minor all go into the same queue (confirmed
    design decision - README section 8, #1). There is no separate lane
    for Minor.

    Backed by heapq. The heap key is (severity.value, insertion_order,
    fault): severity first (Critical=1 sorts before Major=2 before
    Minor=3), then a monotonically increasing insertion counter as a
    stable tiebreaker so two faults of equal severity are handled in
    arrival order. The counter (not created_at) is what's actually
    compared, specifically so heapq never needs to compare two Fault
    objects directly (which would require Fault to implement ordering).
    """

    def __init__(self):
        self._heap: List[Tuple[int, int, Fault]] = []
        self._counter = itertools.count()

    def push(self, fault: Fault) -> None:
        """Add a fault to the queue at a position determined by its severity."""
        heapq.heappush(self._heap, (fault.severity.value, next(self._counter), fault))

    def pop_most_severe(self) -> Fault:
        """Remove and return the most severe fault currently queued."""
        if not self._heap:
            raise IndexError("No faults in the queue.")
        _, _, fault = heapq.heappop(self._heap)
        return fault

    def peek(self) -> Fault:
        """Return the most severe fault without removing it."""
        if not self._heap:
            raise IndexError("No faults in the queue.")
        return self._heap[0][2]

    def iter_by_severity(self) -> Iterator[Fault]:
        """
        Read-only generator over all queued faults in priority order (most
        severe first), without popping them. Takes a sorted copy of the
        heap's contents - heapq only guarantees the root is the smallest,
        not that the whole list is ordered, so a plain iteration over the
        internal heap would NOT be in priority order.
        """
        for _, _, fault in sorted(self._heap):
            yield fault

    def __iter__(self) -> Iterator[Fault]:
        return self.iter_by_severity()

    def __len__(self) -> int:
        return len(self._heap)

    def remove(self, fault: Fault) -> bool:
        """
        Remove a specific fault from the queue - e.g. once it's been
        closed and is no longer "waiting to be addressed". Returns True
        if the fault was found and removed, False if it wasn't queued
        (already removed, or never pushed) - a safe no-op either way.
        """
        for i, (_, _, existing) in enumerate(self._heap):
            if existing is fault:
                del self._heap[i]
                heapq.heapify(self._heap)
                return True
        return False

    def reprioritize(self, fault: Fault) -> bool:
        """
        Re-position a fault already in the queue after its severity
        changed externally (see Fault.change_severity()). heapq has no
        O(log n) "update priority" operation, so this removes and
        re-pushes the fault, which also gives it a fresh insertion-order
        tiebreaker. Returns False (no-op) if the fault isn't currently
        queued - e.g. it was already closed and removed.
        """
        if self.remove(fault):
            self.push(fault)
            return True
        return False
