"""
Unit tests for EventBroadcaster's role audiences (app/events.py): an event
published to roles={"admin"} reaches admin tabs only; one published without
roles reaches every tab; the acting tab is still excluded.

Run from backend/:  python -m pytest tests/test_events_audience.py -v
Runs a real asyncio event loop, because publish() hands events to the loop
with call_soon_threadsafe().
"""

import asyncio

from app.events import EventBroadcaster


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def deliver(publish_calls):
    """Subscribe three tabs, run the publish calls, let the loop deliver, return what each tab got."""
    broadcaster = EventBroadcaster()
    broadcaster.bind_loop(asyncio.get_running_loop())
    queues = {
        "admin-tab": broadcaster.subscribe("admin-tab", role="admin"),
        "user-tab": broadcaster.subscribe("user-tab", role="user"),
        "unknown-tab": broadcaster.subscribe("unknown-tab"),
    }
    for kwargs in publish_calls:
        broadcaster.publish(**kwargs)
    await asyncio.sleep(0)            # let the scheduled put_nowait calls run
    received = {}
    for name, queue in queues.items():
        received[name] = []
        while not queue.empty():
            received[name].append(queue.get_nowait())
    return received


def test_events_without_roles_reach_everyone():
    got = run(deliver([{"event": {"n": 1}}]))
    assert got == {"admin-tab": [{"n": 1}], "user-tab": [{"n": 1}], "unknown-tab": [{"n": 1}]}


def test_admin_only_events_reach_admin_tabs_only():
    got = run(deliver([{"event": {"scope": "sites"}, "roles": {"admin"}}]))
    assert got == {"admin-tab": [{"scope": "sites"}], "user-tab": [], "unknown-tab": []}


def test_the_acting_tab_is_still_excluded():
    got = run(deliver([{"event": {"n": 1}, "roles": {"admin"}, "exclude_client_id": "admin-tab"}]))
    assert got["admin-tab"] == []


def test_publish_without_a_loop_is_a_no_op():
    broadcaster = EventBroadcaster()
    broadcaster.subscribe("tab", role="admin")
    broadcaster.publish({"n": 1}, roles={"admin"})   # must not raise
