"""
AnalysisWorker: the AI analysis queue and the ONE background thread that
works through it.

Producer/consumer design:
- Producers are API request threads (app/api/analysis.py): request() adds a
  job to the back of a FIFO queue, cancel() takes one out.
- The consumer is a single dedicated thread named THREAD_NAME. It takes the
  oldest job, asks the configured AI provider about that fault, posts the
  answer as comments, waits `delay_seconds`, and moves on. So there is never
  more than one AI request in flight, and the API is never called back to
  back.

The queue is a collections.deque guarded by a threading.Condition: the
worker sleeps on the condition while the queue is empty, and request()
wakes it up. (A deque rather than queue.Queue because cancel() must be able
to remove a job from the middle, and the page needs each job's position.)

Only one worker thread can exist: start() refuses if a live thread named
THREAD_NAME already exists. One AnalysisWorker is created per process at
startup (app/api/app.py's lifespan, via app/ai/setup.py).

What happens to a job:
1. request(): adds the "<user> requested <provider> analysis." comment, saves
   the job to SQLite (app/ai/job_store.py) and queues it. One pending job per
   fault - asking again while one is queued or running is refused.
2. When it reaches the front: if its fault no longer exists or is closed,
   it is dropped (one line in the server log, no comment). A fault that was
   closed and then reopened before its turn is analyzed normally.
3. Otherwise the prompt is built from the fault AS IT IS NOW (app/ai/prompt.py)
   and sent to the provider. It tries its models in order (best first) and
   falls back to the next one when a model is busy or out of quota; each
   call gives up after the configured timeout. The answer's header names the
   model that actually answered.
4. The answer is split into at most three comments (app/ai/splitter.py) by
   the "ai-assistant" system user; a failure becomes one error comment
   instead. All comments are saved first, then ONE live-update event is
   published, so viewers of the fault get one "this incident changed" prompt.
5. The job is removed from SQLite; the worker waits `delay_seconds` - only
   after a real API call - and takes the next job.

Cancelling a queued job removes it at once. Cancelling the running job marks
it cancelled: its API call finishes (or times out) in the background and the
answer is thrown away. Either way the fault's buttons reset immediately.

Live updates: besides the normal incident events (for comments), every queue
change publishes one small "ai_analysis" event with the whole queue
({"running": id or null, "queued": [ids in order]}), which fault pages use to
update their Analyze/Cancel buttons in place - no refresh prompt, no toast.
"""

import threading
import traceback
from collections import deque
from typing import Deque, Optional

from app.ai.jobs import AnalysisJob
from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.prompt import build_incident_data, build_instructions
from app.ai.providers.base import AnalysisError, AnalysisProvider
from app.ai.splitter import error_comment, split_answer, total_answer_budget
from app.events import EventBroadcaster, incident_event
from app.models import Fault, IncidentStatus, User
from app.models.system_users import AI_USERNAME

THREAD_NAME = "ai-analysis-worker"
MAX_QUEUED_JOBS = 50

# Live-update scope of the button-only queue event (see frontend/js/events.js).
QUEUE_EVENT_SCOPE = "ai_analysis"


class AnalysisWorker:
    def __init__(self, state, provider: AnalysisProvider, store: SqliteAnalysisJobStore,
                 broadcaster: EventBroadcaster, delay_seconds: float):
        self._state = state                   # AppState: incidents + users
        self.provider = provider
        self._store = store
        self._broadcaster = broadcaster
        self.delay_seconds = delay_seconds

        self._condition = threading.Condition()   # guards everything below
        self._queue: Deque[AnalysisJob] = deque()
        self._running: Optional[AnalysisJob] = None
        self._stopping = False
        self._stop_event = threading.Event()      # interrupts the pause between requests on shutdown
        self._thread: Optional[threading.Thread] = None

        # Jobs saved before a restart go back in the queue, oldest first.
        self._queue.extend(store.load_all())

    # ------------------------------------------------------------------
    # Called from API request threads
    # ------------------------------------------------------------------

    def request(self, fault: Fault, user: User) -> AnalysisJob:
        """
        Queue an analysis of `fault`, requested by `user`, and add the
        "<user> requested <provider> analysis." comment. The comment is added
        while the queue lock is held, before the worker can see the job, so it
        always comes before the answer in the thread. The caller (the API)
        saves the fault and publishes the event.

        Raises:
            ValueError: if `fault` is not a fault (HTTP 400).
            RuntimeError: if the fault is closed, already has a pending
                analysis, or the queue is full (HTTP 409).
        """
        if not isinstance(fault, Fault):
            raise ValueError("AI analysis is only available for faults.")
        if fault.status == IncidentStatus.CLOSED:
            raise RuntimeError("Only open or in-progress faults can be analyzed.")
        with self._condition:
            if self._pending_job(fault.id) is not None:
                raise RuntimeError("An AI analysis of this fault is already pending.")
            if len(self._queue) >= MAX_QUEUED_JOBS:
                raise RuntimeError(f"The AI analysis queue is full ({MAX_QUEUED_JOBS} faults waiting) - try again later.")
            job = AnalysisJob(incident_id=fault.id, requested_by=user.username)
            fault.add_comment(user, request_comment(user.username, self.provider))
            self._store.add(job)
            self._queue.append(job)
            self._condition.notify()          # wake the worker if it is waiting
        self._publish_queue()
        return job

    def cancel(self, fault: Fault, user: User) -> None:
        """
        Cancel the pending analysis of `fault` (queued or running), and add
        the "<user> requested to cancel <provider> analysis." comment. The
        caller (the API) saves the fault and publishes the event.
        Raises RuntimeError (HTTP 409) if there is none.
        """
        with self._condition:
            job = self._pending_job(fault.id)
            if job is None:
                raise RuntimeError("There is no pending AI analysis of this fault.")
            if job is self._running:
                job.cancelled = True          # the running call finishes; its answer is discarded
            else:
                self._queue.remove(job)
            fault.add_comment(user, cancel_comment(user.username, self.provider))
            self._store.remove(job.id)
        self._publish_queue()

    def status_of(self, incident_id: str) -> dict:
        """{"state": "idle" | "queued" | "running", "position": 1-based place in the waiting line, or None}."""
        with self._condition:
            job = self._pending_job(incident_id)
            if job is None:
                return {"state": "idle", "position": None}
            if job is self._running:
                return {"state": "running", "position": None}
            return {"state": "queued", "position": list(self._queue).index(job) + 1}

    def queue_snapshot(self) -> dict:
        """{"running": incident id or None, "queued": [incident ids, next first]} - cancelled jobs are left out."""
        with self._condition:
            running = self._running.incident_id if self._running and not self._running.cancelled else None
            return {"running": running, "queued": [job.incident_id for job in self._queue]}

    # ------------------------------------------------------------------
    # Thread lifecycle (called from app/api/app.py's lifespan)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Refuses if a thread with THREAD_NAME is already alive anywhere."""
        if any(thread.name == THREAD_NAME and thread.is_alive() for thread in threading.enumerate()):
            raise RuntimeError(f"A thread named {THREAD_NAME!r} is already running - only one is allowed.")
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """
        Ask the thread to finish and wait up to `timeout` seconds. A job in
        the middle of an API call is not interrupted; the thread is a daemon,
        so it never keeps the process alive, and the job is still in SQLite,
        so it runs again after the next start.
        """
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        self._refresh_models()                # once, before the first job (Gemini model discovery)
        while True:
            job = self._next_job()
            if job is None:                   # stop() was called
                return
            try:
                called_api = self._process(job)
            except Exception:                 # a bug must never kill the worker thread
                traceback.print_exc()
                called_api = False
            self._finish(job)
            if called_api and self._stop_event.wait(self.delay_seconds):
                return                        # stop() was called during the pause

    def _refresh_models(self) -> None:
        """
        Let the provider update its model list (provider.refresh_models() -
        only Gemini with GEMINI_MODEL_DISCOVERY=auto does anything). Runs on
        this worker thread, so the server starts at once and the listing call
        never delays a page. A failure (network down, slow API) is logged and
        the configured list is kept - it never stops the worker.
        """
        try:
            summary = self.provider.refresh_models()
        except Exception as exc:
            print(f"AI analysis: could not refresh the model list ({type(exc).__name__}: {exc}) - "
                  f"keeping {self.provider.label()}.")
            return
        if summary:
            print(f"AI analysis: {summary}")

    def _next_job(self) -> Optional[AnalysisJob]:
        """Block until there is a job (or stop() is called), then make it the running job."""
        with self._condition:
            while not self._queue and not self._stopping:
                self._condition.wait()
            if self._stopping:
                return None
            self._running = self._queue.popleft()
            job = self._running
        self._publish_queue()
        return job

    def _process(self, job: AnalysisJob) -> bool:
        """Analyze one job's fault and post the result. Returns True if the API was called."""
        fault = self._open_fault(job.incident_id)
        if fault is None:
            print(f"AI analysis: dropped the job for incident {job.incident_id} - it is closed or no longer exists.")
            return False

        label = self.provider.label()          # all configured models - used if none answers
        instructions = build_instructions(total_answer_budget())
        incident_data = build_incident_data(fault, AI_USERNAME)
        try:
            # Tries the provider's models in order, falling back when one is busy (providers/base.py).
            result = self.provider.analyze(instructions, incident_data)
            texts, action = split_answer(result.text, self.provider.label(result.model)), "ai_analysis_posted"
        except AnalysisError as exc:
            texts, action = [error_comment(label, str(exc))], "ai_analysis_failed"
        except Exception as exc:              # an unexpected SDK error: still tell the users
            traceback.print_exc()
            texts, action = [error_comment(label, f"unexpected error ({type(exc).__name__})")], "ai_analysis_failed"

        with self._condition:
            if job.cancelled:
                return True                   # cancelled while running: throw the answer away

        ai_user = self._state.users.get(AI_USERNAME)
        for text in texts:
            fault.add_comment(ai_user, text)
        self._state.incidents.save(fault)
        self._broadcaster.publish(incident_event(fault, action, AI_USERNAME))
        return True

    def _finish(self, job: AnalysisJob) -> None:
        with self._condition:
            self._running = None
        self._store.remove(job.id)
        self._publish_queue()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pending_job(self, incident_id: str) -> Optional[AnalysisJob]:
        """The queued or running (and not cancelled) job for this incident, if any. Call with the lock held."""
        if self._running and not self._running.cancelled and self._running.incident_id == incident_id:
            return self._running
        return next((job for job in self._queue if job.incident_id == incident_id), None)

    def _open_fault(self, incident_id: str) -> Optional[Fault]:
        """The fault to analyze, or None if it is gone, not a fault, or closed (the job is then dropped)."""
        try:
            incident = self._state.incidents.get(incident_id)
        except KeyError:
            return None
        if not isinstance(incident, Fault) or incident.status == IncidentStatus.CLOSED:
            return None
        return incident

    def _publish_queue(self) -> None:
        """Tell every tab what the queue looks like now (button state only - see the module docstring)."""
        self._broadcaster.publish({"scope": QUEUE_EVENT_SCOPE, **self.queue_snapshot()})

    def __repr__(self) -> str:
        return f"AnalysisWorker(provider={self.provider!r}, queued={len(self._queue)}, alive={self.is_alive()})"


def request_comment(username: str, provider: AnalysisProvider) -> str:
    """The comment posted when someone asks for an analysis, e.g. 'tech1 requested Gemini analysis.'"""
    return f"{username} requested {provider.display_name} analysis."


def cancel_comment(username: str, provider: AnalysisProvider) -> str:
    """The comment posted when someone cancels one, e.g. 'tech1 requested to cancel Gemini analysis.'"""
    return f"{username} requested to cancel {provider.display_name} analysis."


__all__ = ["AnalysisWorker", "THREAD_NAME", "MAX_QUEUED_JOBS", "QUEUE_EVENT_SCOPE",
           "request_comment", "cancel_comment"]
