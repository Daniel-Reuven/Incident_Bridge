"""Strict FIFO maintenance queue and its manager (README sections 3, 7, 8)."""

from collections import deque
from typing import Dict, Iterator, Optional

from app.models.enums import ResolutionType
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


class MaintenanceQueue:
    """
    Strict FIFO queue for MaintenanceTask incidents, backed by
    collections.deque.

    Deliberately exposes NO reorder / remove-from-middle / swap method -
    only enqueue (append to the back) and start_next / complete_current
    (pop from the front). This is an intentional API restriction, not an
    oversight: the FIFO guarantee can never be bypassed from anywhere else
    in the codebase, not even by an admin (confirmed design decision -
    README section 8, #3).
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
        """Close the in-progress task (via Incident.close) and free the queue for the next task."""
        if self._current is None:
            raise RuntimeError("No task is currently in progress.")
        task = self._current
        task.close(actor, resolution_type, message)
        self._current = None
        self._renumber()
        return task

    def pending_tasks(self) -> Iterator[MaintenanceTask]:
        """Read-only generator over pending tasks in FIFO order. Does not mutate the queue."""
        for task in self._pending:
            yield task

    def __iter__(self) -> Iterator[MaintenanceTask]:
        return self.pending_tasks()

    def __len__(self) -> int:
        return len(self._pending) + (1 if self._current else 0)

    def _renumber(self) -> None:
        """Keep queue_position (display-only) in sync with actual FIFO order."""
        start = 2 if self._current else 1
        for i, task in enumerate(self._pending, start=start):
            task.queue_position = i
        if self._current is not None:
            self._current.queue_position = 1


class MaintenanceQueueManager:
    """
    Holds one or more named MaintenanceQueue instances.

    Today only a single "default" queue is used (confirmed design
    decision - README section 8, #5). This manager exists so that a
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
