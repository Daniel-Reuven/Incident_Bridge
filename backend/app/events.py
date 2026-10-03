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
"""
import asyncio
from typing import Dict, Iterable, NamedTuple, Optional


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

    def __str__(self) -> str:
        return f"EventBroadcaster(subscribers={len(self._subscribers)}, loop_bound={self._loop is not None})"