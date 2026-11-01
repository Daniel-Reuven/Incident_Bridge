"""
Tests for the clean-shutdown support in app/events.py: EventBroadcaster.close()
ends every live-update stream (END_OF_STREAM), and install_shutdown_hook()
wraps the existing Ctrl+C / SIGTERM handlers so close() runs first.

Run from backend/:  python -m pytest tests/test_events_shutdown.py -v
"""

import asyncio
import signal
import threading

from app.events import END_OF_STREAM, EventBroadcaster, install_shutdown_hook


def test_close_ends_every_open_stream_and_streams_opened_later():
    async def scenario():
        broadcaster = EventBroadcaster()
        broadcaster.bind_loop(asyncio.get_running_loop())
        first = broadcaster.subscribe("tab-1", role="admin")
        second = broadcaster.subscribe("tab-2", role="user")
        broadcaster.close()
        broadcaster.close()                      # calling twice is harmless
        await asyncio.sleep(0)
        late = broadcaster.subscribe("tab-3")    # opened while shutting down
        return [q.get_nowait() for q in (first, second, late)], [q.qsize() for q in (first, second, late)]

    items, remaining = asyncio.new_event_loop().run_until_complete(scenario())
    assert all(item is END_OF_STREAM for item in items)
    assert remaining == [0, 0, 0]                # exactly one END_OF_STREAM each


def test_close_without_a_loop_is_harmless():
    EventBroadcaster().close()


def test_hook_wraps_the_previous_handler_and_closes_first():
    calls = []
    original = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: calls.append("previous handler"))

        class Spy(EventBroadcaster):
            def close(self):
                calls.append("close")

        assert install_shutdown_hook(Spy()) is True
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert calls == ["close", "previous handler"]
    finally:
        signal.signal(signal.SIGTERM, original)


def test_hook_does_nothing_outside_the_main_thread():
    result = []
    thread = threading.Thread(target=lambda: result.append(install_shutdown_hook(EventBroadcaster())))
    thread.start()
    thread.join()
    assert result == [False]
