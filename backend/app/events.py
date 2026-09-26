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
"""

import asyncio
from typing import Dict, Optional


class EventBroadcaster:
    """
    One instance lives on app.state for the process's lifetime (see
    app/api/app.py's lifespan). Subscribers are keyed by the browser tab's
    self-chosen client_id (see frontend/js/events.js) rather than a
    server-generated id, since that's also the id used to exclude a tab
    from receiving its own broadcasts (see publish()'s exclude_client_id).
    """

    def __init__(self):
        self._subscribers: Dict[str, "asyncio.Queue[dict]"] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once at startup, from inside the running event loop (see app/api/app.py)."""
        self._loop = loop

    def subscribe(self, client_id: str) -> "asyncio.Queue[dict]":
        """
        A tab reconnecting with the same client_id (e.g. navigating from
        the dashboard to an incident page) simply replaces its own old
        queue here; the old SSE connection just goes quiet until the
        browser tears it down, rather than anything crashing.
        """
        queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._subscribers[client_id] = queue
        return queue

    def unsubscribe(self, client_id: str) -> None:
        self._subscribers.pop(client_id, None)

    def publish(self, event: dict, exclude_client_id: Optional[str] = None) -> None:
        """
        Safe to call from any thread. Silently does nothing if no event
        loop has been bound yet (e.g. a test that builds the domain layer
        directly without going through the FastAPI lifespan) - live
        updates are a UI nicety, not something correctness depends on.
        """
        if self._loop is None:
            return
        for client_id, queue in list(self._subscribers.items()):
            if client_id == exclude_client_id:
                continue
            self._loop.call_soon_threadsafe(queue.put_nowait, event)
