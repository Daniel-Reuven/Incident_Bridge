"""
GET /events - the Server-Sent Events stream browser tabs connect to for
live updates (see app/events.py for the broadcaster this reads from, and
frontend/js/events.js for the client side).

Deliberately SSE, not WebSockets: the browser only ever needs to be told
"something changed, here's what" - it never needs to push data upstream
over this particular channel, since mutations still go through the normal
REST endpoints in app/api/incidents.py. EventSource gives us one-way
push with automatic reconnection built into the browser, for a lot less
code and protocol surface than a bidirectional WebSocket would need.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from app.api.deps import get_broadcaster, get_current_user
from app.events import EventBroadcaster
from app.models import User

router = APIRouter(tags=["events"])

# How long to wait for a new event before sending a keepalive comment.
# Without this, an idle connection can be silently dropped by an
# intermediary (a proxy, a load balancer - relevant once this is deployed
# behind Render) well before either side considers it closed.
_KEEPALIVE_SECONDS = 20.0


@router.get("/events")
async def stream_events(
    request: Request,
    client_id: str,
    broadcaster: EventBroadcaster = Depends(get_broadcaster),
    _current_user: User = Depends(get_current_user),
):
    """
    client_id is a query parameter rather than a header because
    EventSource cannot set custom request headers - the browser cookie
    still handles authentication as normal (EventSource sends cookies on
    same-origin requests automatically, exactly like fetch does).
    """
    queue = broadcaster.subscribe(client_id)

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(client_id)

    return _EventStreamResponse(event_stream())


class _EventStreamResponse(StreamingResponse):
    """
    Thin subclass purely to fix the response headers SSE needs (no-cache,
    keep-alive, and disabling proxy buffering) in one place rather than
    repeating them if a second SSE endpoint is ever added.
    """

    def __init__(self, content):
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
