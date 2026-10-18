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

Started on demand (v0.4.5): creating the worker at startup starts NO thread
and creates NO provider. The thread starts with the FIRST request (request() starts
it while holding the queue lock, so two simultaneous first requests still
start exactly one thread), or at startup when jobs were restored from SQLite
(start_if_restored()). The thread's first steps are to create the provider
and its HTTP client (ProviderLoader in
app/ai/providers/loader.py) - and then Gemini model discovery. Until then the
page gets the provider's name and models from the settings. Once started,
the thread runs until the server shuts down (it sleeps while the queue is
empty, using no CPU); it never stops on its own - a sleeping thread costs
nothing, so stopping would save nothing.

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

Re-analysis rule (app/ai/reanalysis.py): after a SUCCESSFUL analysis, the
same fault cannot be analyzed again with the same provider until someone
changes it (a comment, status, severity, assignee...). An admin can force it
anyway, but only after the cooldown (AI_REANALYSIS_COOLDOWN_SECONDS, default
5 minutes) since that analysis. Each posted answer is remembered in SQLite
(record_success), so the rule survives restarts.

Cancelling a queued job removes it at once. Cancelling the running job marks
it cancelled: its API call finishes (or times out) in the background and the
answer is thrown away. Either way the fault's buttons reset immediately.

Request history (app/ai/request_history.py): every request gets a permanent
row (unlike analysis_jobs, which only holds pending jobs) - requested when
the job is queued, filled in with its outcome (success, success_unstructured,
failed, cancelled or dropped), the model, and any parsed findings once the
worker is done with it.

Listeners (app/analysis_service.py, the Observer pattern): after an answer
with valid structured findings is posted (outcome success, never
success_unstructured or failed), every listener passed in at construction
is called with one AnalysisCompleted. A listener that raises is logged and
never stops the others, or the comments already posted.

Console log: every step above is logged (app/ai/log.py) - each request,
refusal, cancel, job start and end, model attempt and pause - and every
queue change ends with the queue's state, e.g. "| queue: running seed-075,
2 waiting", so the console always shows what the queue is doing.

Live updates: besides the normal incident events (for comments), every queue
change publishes one small "ai_analysis" event with the whole queue
({"running": id or null, "queued": [ids in order]}), which fault pages use to
update their Analyze/Cancel buttons in place - no refresh prompt, no toast.
"""

import threading
import time
import traceback
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Deque, Iterable, List, Optional, Tuple, Union

from app.analysis_service import AnalysisCompleted, AnalysisListener, notify_listeners
from app.ai.jobs import AnalysisJob
from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.log import log, short_title
from app.ai.prompt import build_incident_data, build_instructions
from app.ai.providers.base import AnalysisError, AnalysisProvider
from app.ai.providers.loader import ProviderLoader
from app.ai.reanalysis import DEFAULT_COOLDOWN_SECONDS, Decision, LastAnalysis, analysis_fingerprint, decide, wait_text
from app.ai.request_history import (OUTCOME_CANCELLED, OUTCOME_DROPPED, OUTCOME_FAILED, OUTCOME_SUCCESS,
                                    OUTCOME_SUCCESS_UNSTRUCTURED, SqliteRequestHistoryStore)
from app.ai.result import AnalysisFindings, extract_findings
from app.ai.splitter import error_comment, split_answer, total_answer_budget
from app.events import EventBroadcaster, incident_event
from app.models import Fault, IncidentStatus, Role, User
from app.models.system_users import AI_USERNAME

THREAD_NAME = "ai-analysis-worker"
MAX_QUEUED_JOBS = 50

# Live-update scope of the button-only queue event (see frontend/js/events.js).
QUEUE_EVENT_SCOPE = "ai_analysis"


class AnalysisWorker:
    def __init__(self, state, provider: Union[ProviderLoader, AnalysisProvider], store: SqliteAnalysisJobStore,
                 broadcaster: EventBroadcaster, delay_seconds: float,
                 cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS, start_on_request: bool = True,
                 listeners: Iterable[AnalysisListener] = ()):
        """
        `provider`: a ProviderLoader (the app - the provider is created on the
        first analysis) or a ready AnalysisProvider (tests).
        `start_on_request`: start the thread on the first request (the app);
        False leaves starting to start() - tests use it to inspect the queue.
        `listeners`: notified (app/analysis_service.py) after an answer with
        valid structured findings is posted; empty by default, so every
        existing caller is unaffected.
        Starts no thread itself; restores saved jobs (see start_if_restored()).
        """
        self._state = state                   # AppState: incidents + users
        self._loader = provider if isinstance(provider, ProviderLoader) else ProviderLoader.ready(provider)
        self._start_on_request = start_on_request
        self._store = store
        self._history = SqliteRequestHistoryStore(store.db_path)
        self._listeners = list(listeners)
        self._broadcaster = broadcaster
        self.delay_seconds = delay_seconds
        self.cooldown_seconds = cooldown_seconds   # before an admin may force a re-analysis

        self._condition = threading.Condition()   # guards everything below
        self._queue: Deque[AnalysisJob] = deque()
        self._running: Optional[AnalysisJob] = None
        self._stopping = False
        self._stop_event = threading.Event()      # interrupts the pause between requests on shutdown
        self._thread: Optional[threading.Thread] = None

        self._restore_jobs()

    def _restore_jobs(self) -> None:
        """Jobs saved before a restart go back in the queue, oldest first (start_if_restored() runs them)."""
        self._queue.extend(self._store.load_all())
        if self._queue:
            log.info(f"Restored {len(self._queue)} queued job(s) from the database: "
                     f"{', '.join(job.incident_id for job in self._queue)}")

    # ------------------------------------------------------------------
    # The provider (created on the first analysis - see the module docstring)
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        """The provider's display name ("Gemini") - from the settings, no provider needed."""
        return self._loader.display_name

    @property
    def models(self) -> List[str]:
        """The model list, best first: from the settings until the provider exists, then its live list."""
        return self._loader.models

    @property
    def provider(self) -> Optional[AnalysisProvider]:
        """The provider once created (by the worker thread), else None."""
        return self._loader.provider

    @provider.setter
    def provider(self, provider: AnalysisProvider) -> None:
        """Replace the provider (tests swap in a slower fake one)."""
        self._loader.set(provider)

    @property
    def provider_loaded(self) -> bool:
        """True once the provider (and its HTTP client) has been created."""
        return self._loader.loaded

    # ------------------------------------------------------------------
    # Called from API request threads
    # ------------------------------------------------------------------

    def request(self, fault: Fault, user: User, force: bool = False) -> AnalysisJob:
        """
        Queue an analysis of `fault`, requested by `user`, and add the
        "<user> requested <provider> analysis." comment (with `force`, an
        admin's re-analysis: "... analysis again (forced)."). The re-analysis
        rule applies (app/ai/reanalysis.py; see the module docstring). The comment is added
        while the queue lock is held, before the worker can see the job, so it
        always comes before the answer in the thread. The caller (the API)
        saves the fault and publishes the event.

        Raises:
            ValueError: if `fault` is not a fault (HTTP 400).
            PermissionError: if `force` is used by a non-admin (HTTP 403).
            RuntimeError: if the fault is closed, already has a pending
                analysis, was already analyzed with no changes since (and is
                not forced, or forced within the cooldown), or the queue is
                full (HTTP 409).
        """
        if not isinstance(fault, Fault):
            raise ValueError("AI analysis is only available for faults.")
        if fault.status == IncidentStatus.CLOSED:
            self._refused(fault, user, "the fault is closed")
            raise RuntimeError("Only open or in-progress faults can be analyzed.")
        with self._condition:
            if self._pending_job(fault.id) is not None:
                self._refused(fault, user, "an analysis is already pending")
                raise RuntimeError("An AI analysis of this fault is already pending.")
            forced = self._check_reanalysis(fault, user, force)
            if len(self._queue) >= MAX_QUEUED_JOBS:
                self._refused(fault, user, f"the queue is full ({MAX_QUEUED_JOBS} waiting)")
                raise RuntimeError(f"The AI analysis queue is full ({MAX_QUEUED_JOBS} faults waiting) - try again later.")
            job = AnalysisJob(incident_id=fault.id, requested_by=user.username)
            fault.add_comment(user, request_comment(user.username, self.provider_name, forced))
            self._store.add(job)
            self._history.record_requested(job, self.provider_name)
            self._queue.append(job)
            position = len(self._queue)
            self._condition.notify()          # wake the worker if it is waiting
            if self._start_on_request and self._thread is None and not self._stopping:
                self.start("first request")   # under the lock: exactly one thread, even for two first requests
        log.info(f"Queued fault {fault.id} {short_title(fault.title)} for {user.username}"
                 f"{' (forced re-analysis)' if forced else ''} - position {position} | {self.queue_summary()}")
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
                what = "it was running - the call will finish and its answer will be discarded"
            else:
                what = f"it was waiting at position {list(self._queue).index(job) + 1}"
                self._queue.remove(job)
                self._history.record_finished(job.id, OUTCOME_CANCELLED)
            fault.add_comment(user, cancel_comment(user.username, self.provider_name))
            self._store.remove(job.id)
        log.info(f"Cancelled fault {fault.id} by {user.username} - {what} | {self.queue_summary()}")
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

    def reanalysis_status(self, fault: Fault, user: User) -> dict:
        """
        The re-analysis rule for `fault` as seen by `user`, for the page:
          last_analysis       {"provider", "model", "at" (ISO time)} or None
          blocked             True: a normal request would be refused
          reason              why (shown under the buttons), or None
          can_force           True: this user (an admin) may Re-analyze now
          force_wait_seconds  > 0: seconds until this admin may Re-analyze
        """
        last = self._store.last_success(fault.id)
        decision = self._decision(fault, user, last)
        return {
            "last_analysis": None if last is None else
            {"provider": last.provider, "model": last.model, "at": last.finished_at.isoformat()},
            "blocked": not decision.allowed,
            "reason": decision.reason,
            "can_force": decision.can_force,
            "force_wait_seconds": decision.force_wait_seconds,
        }

    def queue_snapshot(self) -> dict:
        """{"running": incident id or None, "queued": [incident ids, next first]} - cancelled jobs are left out."""
        with self._condition:
            running = self._running.incident_id if self._running and not self._running.cancelled else None
            return {"running": running, "queued": [job.incident_id for job in self._queue]}

    def queue_summary(self) -> str:
        """The queue in one log-friendly phrase, e.g. 'queue: running seed-075, 2 waiting (seed-076, seed-079)'."""
        snapshot = self.queue_snapshot()
        running = f"running {snapshot['running']}" if snapshot["running"] else "idle"
        waiting = snapshot["queued"]
        listed = f" ({', '.join(waiting[:5])}{', …' if len(waiting) > 5 else ''})" if waiting else ""
        return f"queue: {running}, {len(waiting)} waiting{listed}"

    # ------------------------------------------------------------------
    # Thread lifecycle: started by the first request() or start_if_restored(),
    # stopped by app/api/app.py's lifespan at shutdown
    # ------------------------------------------------------------------

    def start(self, reason: str = "") -> None:
        """
        Start the worker thread. Refuses if a thread with THREAD_NAME is
        already alive anywhere. `reason` is only for the log line
        ("first request", "restored jobs").
        """
        with self._condition:                 # re-entrant: request() calls this with the lock held
            if any(thread.name == THREAD_NAME and thread.is_alive() for thread in threading.enumerate()):
                raise RuntimeError(f"A thread named {THREAD_NAME!r} is already running - only one is allowed.")
            self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
            self._thread.start()
        log.info(f"Worker thread {THREAD_NAME} started{f' ({reason})' if reason else ''} - "
                 f"{self._loader.label()}, {self.delay_seconds:g}s pause between jobs | {self.queue_summary()}")

    def start_if_restored(self) -> bool:
        """Start the thread now if jobs were restored from SQLite (they should not wait for a new request)."""
        with self._condition:
            if not self._queue or self._thread is not None:
                return False
            self.start("restored jobs")
            return True

    @property
    def started(self) -> bool:
        """True once the thread has been started (it then runs until stop())."""
        return self._thread is not None

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
            busy = self._running is not None
        if self._thread is None:
            log.info(f"Worker thread {THREAD_NAME} was never started - nothing to stop | {self.queue_summary()}")
            return
        log.info(f"Worker thread {THREAD_NAME} stopping"
                 + (" - a call is still running; that job stays saved and runs again after the next start" if busy else "")
                 + f" | {self.queue_summary()}")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        if self._load_provider():             # create the provider and its HTTP client, once
            self._refresh_models()            # then Gemini model discovery, before the first job
        while True:
            job = self._next_job()
            if job is None:                   # stop() was called
                return
            started = time.monotonic()
            try:
                called_api, outcome = self._process(job)
            except Exception:                 # a bug must never kill the worker thread
                traceback.print_exc()
                called_api, outcome = False, "crashed - see the traceback above"
                self._history.record_finished(job.id, OUTCOME_FAILED)
            self._finish(job, outcome, time.monotonic() - started)
            if called_api:
                log.debug(f"Pausing {self.delay_seconds:g}s before the next job")
                if self._stop_event.wait(self.delay_seconds):
                    return                    # stop() was called during the pause

    def _load_provider(self) -> bool:
        """
        Create the provider and its HTTP client - the first
        step of the thread. Returns False if that failed: it is logged, and
        each job then tries again and posts an error comment if it still fails.
        """
        if self._loader.loaded:
            return True
        started = time.monotonic()
        try:
            self._loader.get()
        except Exception as exc:
            log.error(f"Could not create the {self.provider_name} provider ({type(exc).__name__}: {exc})")
            return False
        log.info(f"Created the {self.provider_name} provider and its HTTP client in {time.monotonic() - started:.1f}s")
        return True

    def _refresh_models(self) -> None:
        """
        Let the provider update its model list (provider.refresh_models() -
        only Gemini with GEMINI_MODEL_DISCOVERY=auto does anything). Runs on
        this worker thread, so the server starts at once and the listing call
        never delays a page. A failure (network down, slow API) is logged and
        the configured list is kept - it never stops the worker.
        """
        try:
            summary = self._loader.get().refresh_models()
        except Exception as exc:
            log.warning(f"Could not refresh the model list ({type(exc).__name__}: {exc}) - "
                        f"keeping {self._loader.label()}")
            return
        if summary:
            log.info(summary)

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

    def _process(self, job: AnalysisJob) -> Tuple[bool, str]:
        """
        Analyze one job's fault and post the result. Returns (whether the API
        was called, a short outcome for the log line).
        """
        fault = self._open_fault(job.incident_id)
        if fault is None:
            log.warning(f"Dropped the job for fault {job.incident_id} - it is closed or no longer exists")
            self._history.record_finished(job.id, OUTCOME_DROPPED)
            return False, "dropped (closed or gone)"

        label = self._loader.label()           # all configured models - used if none answers
        instructions = build_instructions(total_answer_budget())
        incident_data = build_incident_data(fault, AI_USERNAME)
        fingerprint = analysis_fingerprint(fault, AI_USERNAME)   # of the data this answer is based on
        waited = (datetime.now(timezone.utc) - job.requested_at).total_seconds()
        log.info(f"Started fault {fault.id} {short_title(fault.title)} (requested by {job.requested_by}, "
                 f"waited {waited:.1f}s) - {len(fault.comments)} comments, "
                 f"{len(incident_data):,} characters of data | {self.queue_summary()}")
        result = None
        findings = None
        try:
            provider = self._loader.get()     # already created by _run(); tried again here if that failed
        except Exception as exc:
            log.error(f"Could not create the {self.provider_name} provider ({type(exc).__name__}: {exc})")
            texts = [error_comment(label, f"the {self.provider_name} provider could not be started "
                                          f"({type(exc).__name__}) - see the server log")]
            return self._post(job, fault, texts, "ai_analysis_failed", fingerprint, None, None, called_api=False)
        try:
            # Tries the provider's models in order, falling back when one is busy (providers/base.py).
            result = provider.analyze(instructions, incident_data)
            text, findings = extract_findings(result.text)   # strips the trailing JSON block, if any (app/ai/result.py)
            texts, action = split_answer(text, provider.label(result.model)), "ai_analysis_posted"
        except AnalysisError as exc:
            texts, action = [error_comment(label, str(exc))], "ai_analysis_failed"
        except Exception as exc:              # an unexpected error: still tell the users
            traceback.print_exc()
            texts, action = [error_comment(label, f"unexpected error ({type(exc).__name__})")], "ai_analysis_failed"

        return self._post(job, fault, texts, action, fingerprint, result, findings, called_api=True)

    def _post(self, job: AnalysisJob, fault: Fault, texts: List[str], action: str, fingerprint: str,
              result, findings: Optional[AnalysisFindings], called_api: bool) -> Tuple[bool, str]:
        """
        Post the answer (or error) comments - unless the job was cancelled
        meanwhile - remember a success, and publish ONE incident event.
        Returns (called_api, outcome) for _process().
        """
        with self._condition:
            if job.cancelled:                 # cancelled while running: throw the answer away
                self._history.record_finished(job.id, OUTCOME_CANCELLED)
                return called_api, "answer discarded (cancelled while running)"

        ai_user = self._state.users.get(AI_USERNAME)
        for text in texts:
            fault.add_comment(ai_user, text)
        self._state.incidents.save(fault)
        if action == "ai_analysis_posted":     # only a posted answer counts for the re-analysis rule
            self._store.record_success(fault.id, LastAnalysis(
                self.provider_name, result.model, fingerprint, datetime.now(timezone.utc)))
            outcome = OUTCOME_SUCCESS if findings is not None else OUTCOME_SUCCESS_UNSTRUCTURED
            self._history.record_finished(job.id, outcome, model=result.model,
                                           findings=asdict(findings) if findings is not None else None)
            if findings is not None:           # only a valid, structured answer is worth telling listeners about
                notify_listeners(self._listeners, AnalysisCompleted(fault, findings, self.provider_name, result.model))
        else:
            self._history.record_finished(job.id, OUTCOME_FAILED)
        self._broadcaster.publish(incident_event(fault, action, AI_USERNAME))
        if action == "ai_analysis_failed":
            return called_api, "posted an error comment"
        return called_api, f"posted {len(texts)} comment{'s' if len(texts) > 1 else ''}"

    def _finish(self, job: AnalysisJob, outcome: str, seconds: float) -> None:
        """Clear the running job, remove it from SQLite, log how it ended, and publish the queue."""
        with self._condition:
            self._running = None
        self._store.remove(job.id)
        log.info(f"Finished fault {job.incident_id}: {outcome} ({seconds:.1f}s) | {self.queue_summary()}")
        self._publish_queue()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _decision(self, fault: Fault, user: User, last: Optional[LastAnalysis] = None) -> Decision:
        """decide() for this fault, user and the provider configured now (app/ai/reanalysis.py)."""
        if last is None:
            last = self._store.last_success(fault.id)
        return decide(last, analysis_fingerprint(fault, AI_USERNAME), self.provider_name,
                      user.role == Role.ADMIN, self.cooldown_seconds)

    def _check_reanalysis(self, fault: Fault, user: User, force: bool) -> bool:
        """
        Apply the re-analysis rule to a new request. Returns True if it goes
        ahead as a FORCED re-analysis, False for a normal one; raises the
        error the user sees otherwise. Called with the lock held.
        """
        decision = self._decision(fault, user)
        if decision.allowed:
            return False
        if not force:
            self._refused(fault, user, "already analyzed, no changes since")
            hint = " Use Re-analyze to run it again." if user.role == Role.ADMIN else ""
            raise RuntimeError(decision.reason + hint)
        if user.role != Role.ADMIN:
            self._refused(fault, user, "only an admin can force a re-analysis")
            raise PermissionError("Only an admin can run the AI analysis again without changes.")
        if not decision.can_force:
            wait = wait_text(decision.force_wait_seconds)
            self._refused(fault, user, f"re-analysis cooldown, {wait} left")
            raise RuntimeError(f"{decision.reason} It can be run again in {wait} "
                               f"({self.cooldown_seconds / 60:g}-minute cooldown).")
        return True

    def _refused(self, fault: Fault, user: User, reason: str) -> None:
        """Log a refused request (the caller then raises the error the user sees)."""
        log.info(f"Refused a request for fault {fault.id} by {user.username} - {reason}")

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
        return f"AnalysisWorker(provider={self._loader!r}, queued={len(self._queue)}, alive={self.is_alive()})"


def request_comment(username: str, provider_name: str, forced: bool = False) -> str:
    """
    The comment posted when someone asks for an analysis, e.g. 'tech1 requested
    Gemini analysis.', or for an admin's forced re-analysis 'admin requested
    Gemini analysis again (forced).' (app/ai/reanalysis.py recognises both).
    `provider_name` is the provider's display name, e.g. "Gemini".
    """
    if forced:
        return f"{username} requested {provider_name} analysis again (forced)."
    return f"{username} requested {provider_name} analysis."


def cancel_comment(username: str, provider_name: str) -> str:
    """The comment posted when someone cancels one, e.g. 'tech1 requested to cancel Gemini analysis.'"""
    return f"{username} requested to cancel {provider_name} analysis."


__all__ = ["AnalysisWorker", "THREAD_NAME", "MAX_QUEUED_JOBS", "QUEUE_EVENT_SCOPE",
           "request_comment", "cancel_comment"]
