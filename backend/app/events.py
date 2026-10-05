"""
Live-update broadcast system (Server-Sent Events).

Route handlers in this app are plain sync `def`s (FastAPI runs each one in
a thread pool worker), but the endpoint that streams events out to
browsers (app/api/events.py) is naturally async: it's a long-lived
connection per browser tab that spends almost all its time just waiting,
which suits asyncio far better than holding a whole thread per tab.

That split is exactly why this module needs both threading and asyncio
together, and why it's the second genuine (non-decorative) use of
threading in this project, alongside the SQLite lock in
app/persistence.py: a sync request handler, running in some worker
thread, calls publish() right after a mutation succeeds. The event then
has to reach async consumers (asyncio.Queue.get() loops) running on the
event loop, in a *different* thread. asyncio.Queue is not safe to write
to from another thread directly - loop.call_soon_threadsafe() is the
supported way to hand work to the loop from outside it.

Audiences: each subscriber is registered with its user's role, and
publish() can be limited to certain roles. The incident events go to
everyone (no `roles`), while the site portal's events (app/api/sites.py)
are published with roles={"admin"}, so regular users' tabs never receive
data from the admin-only portal at all - not even hidden in the browser.
Events also carry a "scope" ("sites" for the portal; incident events have
none, which the frontend treats as "incidents" - see frontend/js/events.js),
so each page only reacts to the kind of events it shows.

Clean shutdown: a live-update stream never ends on its own, and Uvicorn's
graceful shutdown waits for every open connection - so without help,
stopping the server (Ctrl+C) would wait for the timeout and then cancel the
streams, printing a CancelledError traceback. install_shutdown_hook() makes
Ctrl+C (and SIGTERM) first call close(), which tells every open stream to
finish right away (END_OF_STREAM), so the connections close normally and
the server stops at once, with no error. serve_app.py's short
timeout_graceful_shutdown stays only as a safety net.
"""
import asyncio
import signal
import threading
from typing import Dict, Iterable, NamedTuple, Optional

# Put on a subscriber's queue by EventBroadcaster.close(): the stream reading
# that queue (app/api/events.py) ends as soon as it sees this object.
END_OF_STREAM = object()


class _Subscriber(NamedTuple):
    """One connected browser tab: its event queue and its user's role (e.g. "admin", or None if unknown)."""
    queue: "asyncio.Queue[dict]"
    role: Optional[str]


class EventBroadcaster:
    """
    One instance lives on app.state for the process's lifetime (see
    app/api/app.py's lifespan). Subscribers are keyed by the browser tab's
    self-chosen client_id (see frontend/js/events.js) rather than a
    server-generated id, since that's also the id used to exclude a tab
    from receiving its own broadcasts (see publish()'s exclude_client_id).
    """

    def __init__(self):
        self._subscribers: Dict[str, _Subscriber] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once at startup, from inside the running event loop (see app/api/app.py)."""
        self._loop = loop

    def subscribe(self, client_id: str, role: Optional[str] = None) -> "asyncio.Queue[dict]":
        """
        Register a tab and return the queue its events arrive on. `role` is
        the logged-in user's role value (see app/api/events.py); events
        published to specific roles only reach tabs with one of them.

        A tab reconnecting with the same client_id (e.g. navigating from
        the dashboard to an incident page) simply replaces its own old
        queue here; the old SSE connection just goes quiet until the
        browser tears it down, rather than anything crashing.
        """
        queue: "asyncio.Queue[dict]" = asyncio.Queue()
        if self._closed:                       # the server is shutting down:
            queue.put_nowait(END_OF_STREAM)    # end this new stream immediately
        self._subscribers[client_id] = _Subscriber(queue, role)
        return queue

    def unsubscribe(self, client_id: str) -> None:
        self._subscribers.pop(client_id, None)

    def publish(self, event: dict, exclude_client_id: Optional[str] = None,
                roles: Optional[Iterable[str]] = None) -> None:
        """
        Send `event` to every subscribed tab except `exclude_client_id` (the
        tab that caused the change). With `roles`, only tabs whose user has
        one of those roles receive it; without it, every tab does.

        Safe to call from any thread. Silently does nothing if no event
        loop has been bound yet (e.g. a test that builds the domain layer
        directly without going through the FastAPI lifespan) - live
        updates are a UI nicety, not something correctness depends on.
        """
        if self._loop is None:
            return
        allowed = set(roles) if roles is not None else None
        for client_id, subscriber in list(self._subscribers.items()):
            if client_id == exclude_client_id:
                continue
            if allowed is not None and subscriber.role not in allowed:
                continue
            self._loop.call_soon_threadsafe(subscriber.queue.put_nowait, event)

    def close(self) -> None:
        """
        Tell every open stream to finish (the server is shutting down). Safe
        to call from a signal handler or any thread, and more than once;
        streams opened afterwards end immediately too (see subscribe()).
        """
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return
        for subscriber in list(self._subscribers.values()):
            self._loop.call_soon_threadsafe(subscriber.queue.put_nowait, END_OF_STREAM)

    def __str__(self) -> str:
        return f"EventBroadcaster(subscribers={len(self._subscribers)}, loop_bound={self._loop is not None})"


def install_shutdown_hook(broadcaster: EventBroadcaster) -> bool:
    """
    Make Ctrl+C / SIGTERM end every live-update stream first, then do
    whatever the signal did before (normally: Uvicorn's own graceful
    shutdown, which then finds no open streams to wait for).

    Called from the app's lifespan startup (app/api/app.py) - by then Uvicorn
    has installed its signal handlers, so this wraps them instead of
    replacing them; a second Ctrl+C still force-quits as usual. Only
    possible from the main thread (a Python rule for signal handlers), so
    returns False and changes nothing elsewhere - e.g. under the test
    client, which runs the app in a background thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):            # Windows: Ctrl+Break
        signals.append(signal.SIGBREAK)
    for sig in signals:
        previous = signal.getsignal(sig)
        if not callable(previous):             # default / ignored: leave it alone
            continue

        def handler(signum, frame, previous=previous):
            broadcaster.close()
            previous(signum, frame)

        signal.signal(sig, handler)
    return True