"""The indexer's shutdown is bounded (USER_DRIVE_INDEXING_RFC §7.1, §12.5): after SIGTERM it waits at most the
graceful timeout for an in-flight tick, then cancels it and waits for its `finally` (the lease release) before
returning, so a deploy never leaves a lease held for its full length.

Hypercorn's own graceful timeout does not cancel an in-flight request on this Python, so `fake_serve` mimics
exactly that: once the shutdown trigger fires it waits for its request forever."""
import asyncio
import signal
from types import SimpleNamespace

import pytest

import indexer_main


def _fake_serve(events, *, crash=None, request_finishes=False):
    async def fake_serve(app, config, shutdown_trigger):
        if crash is not None:
            raise crash

        async def request():
            events.append("tick started")
            try:
                if request_finishes:
                    await asyncio.sleep(0)
                    return
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0.01)    # stands in for the lease release write
                events.append("lease released")

        handler = asyncio.create_task(request())   # hypercorn runs requests as tasks it does not own
        await shutdown_trigger()
        await asyncio.wait({handler})               # graceful wait that never times out
    return fake_serve


async def _run(serve_fn, *, timeout=0.05, signal_after=0.01):
    shutdown = asyncio.Event()
    config = SimpleNamespace(graceful_timeout=timeout)
    asyncio.get_running_loop().call_later(signal_after, shutdown.set)
    await asyncio.wait_for(indexer_main.serve_until_shutdown(object(), config, shutdown, serve_fn=serve_fn), 5)


async def test_in_flight_tick_is_cancelled_after_the_graceful_timeout_and_its_finally_runs():
    events = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _run(_fake_serve(events))
    assert events == ["tick started", "lease released"]   # released before serve_until_shutdown returned
    assert loop.time() - started < 1


async def test_a_tick_that_finishes_within_the_timeout_is_not_cancelled():
    events = []
    await _run(_fake_serve(events, request_finishes=True), timeout=1)
    assert events == ["tick started", "lease released"]


async def test_a_server_that_fails_to_start_raises():
    with pytest.raises(OSError):
        await _run(_fake_serve([], crash=OSError("address in use")), signal_after=10)


async def test_main_serves_through_the_bounded_shutdown(monkeypatch):
    """Calling hypercorn's `serve` directly from `main` is the unbounded wait this guards against."""
    seen = {}

    async def fake_bounded(app, config, shutdown, **_):
        seen.update(app=app, graceful_timeout=config.graceful_timeout)

    async def direct_serve(*_args, **_kwargs):
        raise AssertionError("main must not await hypercorn's serve directly")

    app = object()
    monkeypatch.setattr(indexer_main, "build_app", lambda: app)
    monkeypatch.setattr(indexer_main, "serve_until_shutdown", fake_bounded)
    monkeypatch.setattr(indexer_main, "serve", direct_serve)
    loop = asyncio.get_running_loop()
    try:
        await indexer_main.main()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    assert seen == {"app": app, "graceful_timeout": indexer_main._GRACEFUL_TIMEOUT_S}
