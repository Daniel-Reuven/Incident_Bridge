"""Strict FIFO maintenance queue and its manager (README.md sections 1, 3, 13)."""

from collections import deque
from typing import Dict, Iterator, Optional, Tuple

from app.iterators import FifoQueueIterator
from app.models.enums import IncidentStatus, ResolutionType
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


class MaintenanceQueue:
    """
    Strict FIFO queue for MaintenanceTask incidents, backed by
    collections.deque.

    The FIFO guarantee is about ORDER, and that can still never be
    bypassed from anywhere else in the codebase, not even by an admin
    (confirmed design decision - README.md section 13, #3): there is
    deliberately NO method that moves, swaps, or re-sorts a task that is
    already in the queue. Tasks normally join at the back (enqueue) and
    are handed out from the front (start_next / start_task /
    complete_current).

    Two controlled exceptions exist, both added for manual status changes
    (Task 2):
      - A task may LEAVE from any position, but only by being CLOSED with a
        reason (close_task). The tasks behind it move up one place each.
      - A CLOSED task that an admin REOPENS may RE-ENTER at a position of
        the admin's choosing (reinsert). The tasks behind it move down one
        place each.
    In both cases the relative order of every other task is untouched -
    nothing already in the queue is ever reordered, only removed from or
    inserted into.

    Positions are numbered the way they are displayed: the in-progress
    task (if any) is position 1, and pending tasks follow. With nothing in
    progress, the first pending task is position 1.
    """

    def __init__(self, name: str = "default"):
        self.name = name
        self._pending: deque = deque()
        self._current: Optional[MaintenanceTask] = None

    def enqueue(self, task: MaintenanceTask) -> None:
        """Add a task to the back of the queue."""
        self._pending.append(task)
        self._renumber()

    @property
    def current_task(self) -> Optional[MaintenanceTask]:
        """The task currently in progress, if any."""
        return self._current

    def start_next(self) -> MaintenanceTask:
        """
        Move the next pending task to 'in progress'.

        Raises RuntimeError if a task is already in progress - it must be
        completed first. This is the enforcement point for "one cannot
        start before the previous task is completed".
        """
        if self._current is not None:
            raise RuntimeError("A task is already in progress; complete it before starting the next one.")
        if not self._pending:
            raise IndexError("No pending maintenance tasks to start.")
        self._current = self._pending.popleft()
        self._current.start_progress()
        self._renumber()
        return self._current

    def start_task(self, task: MaintenanceTask) -> MaintenanceTask:
        """
        Start one SPECIFIC task - used when someone presses Start on a task's
        own page. Strict FIFO still applies: only the first pending task may
        be started, and only while nothing is in progress. This is exactly
        start_next() plus a guard that the task the caller is looking at is
        the one that actually gets started - without it, a stale page could
        start a different task than the one it shows if the queue changed in
        the meantime.

        Raises RuntimeError (the API layer maps this to HTTP 409) if a task
        is already in progress, or if `task` is not the first pending task
        (which also covers a task that is not in this queue at all).
        Nothing is changed when it raises.
        """
        if self._current is not None:
            raise RuntimeError("A task is already in progress; complete it before starting the next one.")
        if not self._pending or self._pending[0] is not task:
            raise RuntimeError("Only the first task in the queue can be started - tasks start strictly in order.")
        return self.start_next()

    def restore_current(self, task: MaintenanceTask) -> None:
        """
        Startup-only: directly sets the in-progress task, bypassing the
        normal start_next()/pending-deque flow entirely. Used exactly once,
        when AppState rebuilds queue state from persisted data after a
        restart (see app/state.py) - never called during normal request
        handling. The no-reorder guarantee this class exists to enforce
        (see the class docstring) is about runtime operation; faithfully
        restoring whatever state already existed before the process
        restarted isn't "reordering" anything.

        Raises RuntimeError if a current task is already set - a restart
        should only ever find at most one IN_PROGRESS task, since that
        invariant was already enforced when the data was first written.
        """
        if self._current is not None:
            raise RuntimeError(
                f"Cannot restore {task.title!r} as the current task - "
                f"{self._current.title!r} is already set as current."
            )
        self._current = task
        task.queue_position = 1

    def complete_current(self, actor: User, message: str,
                         resolution_type: ResolutionType = ResolutionType.RESOLVED) -> MaintenanceTask:
        """
        Close the in-progress task (via Incident.close, which also logs the
        status-change comment) and free the queue for the next task. The
        closed task's queue_position is cleared - it no longer has a place
        in the queue - and the pending tasks are renumbered immediately.
        """
        if self._current is None:
            raise RuntimeError("No task is currently in progress.")
        task = self._current
        task.close(actor, resolution_type, message)
        self._current = None
        task.queue_position = None
        self._renumber()
        return task

    def close_task(self, task: MaintenanceTask, actor: User, message: str,
                   resolution_type: ResolutionType = ResolutionType.RESOLVED) -> MaintenanceTask:
        """
        Close ANY task in this queue, whatever its position: the in-progress
        one (identical to complete_current) or a pending one from the middle
        of the queue. The message is the required reason, and
        Incident.close() records the log comment.

        For a pending task the tasks behind it each move up one place and
        its own queue_position is cleared. Nothing is reordered. The queue
        is only touched AFTER task.close() succeeds, so a rejected close
        (blank message, admin-only resolution by a regular user, already
        closed) leaves the queue exactly as it was. Closing the in-progress
        task does NOT start the next one - it stays pending until
        start_next() is called, same as complete_current().

        Raises:
            RuntimeError: if the task is not in this queue.
            ValueError / PermissionError: from Incident.close().
        """
        if task is self._current:
            return self.complete_current(actor, message, resolution_type)
        if not any(pending is task for pending in self._pending):
            raise RuntimeError("This task is not in this queue.")
        task.close(actor, resolution_type, message)
        self._pending = deque(pending for pending in self._pending if pending is not task)
        task.queue_position = None
        self._renumber()
        return task

    def placement_bounds(self) -> Tuple[int, int]:
        """
        (first, last): the lowest and highest position a (re)inserted task
        may be given right now. With a task in progress, position 1 is taken
        so the lowest is 2; the highest is one past the current end of the
        queue (i.e. "at the back").
        """
        first = 2 if self._current is not None else 1
        return first, first + len(self._pending)

    def check_placement(self, status: IncidentStatus, position: int) -> None:
        """
        Validate, WITHOUT changing anything, that a task with `status` may
        be placed at `position`. The API layer calls this BEFORE reopening
        the task, so an invalid placement is rejected while the task is
        still untouched.

        Rules: only OPEN or IN_PROGRESS tasks belong in a queue; an
        IN_PROGRESS task needs the in-progress slot, so nothing else may be
        in progress and the position must be 1; and the position must lie
        within placement_bounds().

        Raises:
            RuntimeError: IN_PROGRESS requested while another task is in
                progress (the API layer maps this to HTTP 409).
            ValueError: any other invalid combination (HTTP 400).
        """
        first, last = self.placement_bounds()
        if status == IncidentStatus.IN_PROGRESS:
            if self._current is not None:
                raise RuntimeError("A task is already in progress, so a reopened task cannot be put in progress.")
            if position != 1:
                raise ValueError("Only position 1 can be in progress.")
        elif status != IncidentStatus.OPEN:
            raise ValueError("A task can only be placed in the queue as Open or In progress.")
        if not first <= position <= last:
            raise ValueError(f"Position must be between {first} and {last}.")

    def reinsert(self, task: MaintenanceTask, position: int) -> None:
        """
        Put a reopened task back into the queue at `position`, according to
        its CURRENT status (the caller reopens it first): an OPEN task is
        inserted among the pending tasks, an IN_PROGRESS task becomes the
        current one (position 1, so every pending task moves down). The
        tasks behind the insertion point each move down one place;
        everything else keeps its relative order. All positions are
        renumbered immediately.

        Raises:
            RuntimeError: if the task is already in this queue, or
                IN_PROGRESS while another task is in progress.
            ValueError: for an invalid status/position (see check_placement).
        """
        if task in self:
            raise RuntimeError("This task is already in the queue.")
        self.check_placement(task.status, position)
        first, _ = self.placement_bounds()
        if task.status == IncidentStatus.IN_PROGRESS:
            self._current = task
        else:
            self._pending.insert(position - first, task)
        self._renumber()

    def pending_tasks(self) -> Iterator[MaintenanceTask]:
        """Read-only generator over pending tasks in FIFO order. Does not mutate the queue."""
        for task in self._pending:
            yield task

    def all_tasks(self) -> Iterator[MaintenanceTask]:
        """
        Read-only generator over EVERY task in the queue in position order:
        the in-progress task first (if any), then the pending ones. Used by
        the API layer to persist every task's queue_position after a change.
        """
        if self._current is not None:
            yield self._current
        yield from self._pending

    def __iter__(self) -> FifoQueueIterator:
        """
        Makes the queue an Iterable: every call returns a NEW
        FifoQueueIterator (app/iterators.py) over a snapshot of the pending
        tasks in FIFO order, so `for task in queue` works, two iterators
        over the same queue advance independently, and changing the queue
        while iterating is safe. The in-progress task is not included.
        """
        return FifoQueueIterator(self._pending)

    def __len__(self) -> int:
        return len(self._pending) + (1 if self._current else 0)

    def __contains__(self, task: MaintenanceTask) -> bool:
        """True if this exact task object is the current task or is pending (identity, not equality)."""
        return task is self._current or any(pending is task for pending in self._pending)

    def _renumber(self) -> None:
        """Keep queue_position (display-only) in sync with actual FIFO order."""
        start = 2 if self._current else 1
        for i, task in enumerate(self._pending, start=start):
            task.queue_position = i
        if self._current is not None:
            self._current.queue_position = 1

    def __str__(self) -> str:
        """String representation showing queue name, current task, and pending tasks."""
        current_title = f"'{self._current.title}'" if self._current else "None"
        pending_titles = ", ".join(f"'{t.title}'" for t in self)
        return f"MaintenanceQueue('{self.name}', {len(self)} tasks: current={current_title}, pending=[{pending_titles}])"

    def __repr__(self) -> str:
        """Developer view: queue name, the in-progress task's id, and how many tasks are pending."""
        current = self._current.id[:8] if self._current else None
        return f"MaintenanceQueue(name={self.name!r}, current={current!r}, pending={len(self._pending)})"


class MaintenanceQueueManager:
    """
    Holds one or more named MaintenanceQueue instances.

    Today only a single "default" queue is used (confirmed design
    decision - README.md section 13, #5). This manager exists so that a
    future split into per-team or per-system sub-queues needs no
    redesign - just new queue names, requested via get_queue().
    """

    def __init__(self):
        self._queues: Dict[str, MaintenanceQueue] = {"default": MaintenanceQueue("default")}

    def get_queue(self, name: str = "default") -> MaintenanceQueue:
        """Return the named queue, creating it on first request if it doesn't exist yet."""
        if name not in self._queues:
            self._queues[name] = MaintenanceQueue(name)
        return self._queues[name]

    def queue_names(self) -> Iterator[str]:
        """Generator over the names of all queues currently managed."""
        yield from self._queues.keys()

    def queue_of(self, task: MaintenanceTask) -> Optional[MaintenanceQueue]:
        """
        The managed queue this exact task currently belongs to (current or
        pending), or None if it is in no queue - e.g. it is already closed.
        Lets the API layer close a task without hardcoding the "default" queue.
        """
        for queue in self._queues.values():
            if task in queue:
                return queue
        return None

    def __str__(self) -> str:
        """String representation of the manager and its active queue names."""
        queues = ", ".join(f"'{name}'" for name in self.queue_names())
        return f"MaintenanceQueueManager({len(self._queues)} queues: [{queues}])"

    def __repr__(self) -> str:
        """Developer view: every managed queue name with its task count."""
        sizes = {name: len(queue) for name, queue in self._queues.items()}
        return f"MaintenanceQueueManager(queues={sizes!r})"
