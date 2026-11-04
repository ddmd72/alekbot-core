"""The caller closes first (2026-11-04): after RESULT the route ends the response only once the caller has ended its body,
bounded by the run's deadline. A response ended while the request half was open made Cloud Run's front end abort a 26 MB
answer with RST_STREAM INTERNAL_ERROR (spike S4d: the front end closes that half itself)."""
import asyncio
import logging

from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    FrameType, TunnelEngineResult, TunnelResult, TunnelStart, TunnelWindows, decode_result, tunnel_budget,
)
from tests.unit.web.test_sandbox_tunnel_app import (  # noqa: F401  (rig, serve_app, roots are fixtures)
    GUARD_S, SOURCE, WINDOWS, FakeRun, engine_result, rig, roots, serve_app, start_frame,
)


async def _two_windows(run: FakeRun) -> TunnelEngineResult:
    out_dir = run.out_root / "media_out_close"
    out_dir.mkdir()
    files = []
    for index, size in ((0, 300_000), (1, 70_000)):
        path = out_dir / f"window_{index}.opus"
        path.write_bytes(bytes([index + 7]) * size)
        files.append((index, str(path)))
    await run.link.records.put(None)
    media = TunnelWindows(status=SandboxStatus.OK, windows=WINDOWS.windows, eof=True, duration_s=1834.5,
                          reached_bound=False)
    return engine_result(TunnelResult(status=SandboxStatus.OK, media=media), files, str(out_dir))


async def test_after_result_the_response_waits_for_the_callers_end_then_ends(rig):  # noqa: F811
    r = await rig(_two_windows)
    client = await r.call("media.audio_windows", body=start_frame(WINDOWS))
    w0, w1, result = (await client.next_frame(), await client.next_frame(), await client.next_frame())
    assert (w0.kind, w1.kind, result.kind) == (FrameType.WINDOW, FrameType.WINDOW, FrameType.RESULT)
    assert decode_result(result.payload, WINDOWS).status is SandboxStatus.OK

    await asyncio.sleep(0.3)
    assert not client.ended.is_set()                 # the caller's half is open: the response stays open

    client.end_body()
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    assert not client.reset.is_set()
    await r.settled()
    assert r.recycler.calls == 0 and not r.slot.draining          # RESULT `ok` that reached the end keeps it warm


async def test_a_caller_that_never_ends_its_body_gets_the_end_at_the_runs_deadline(rig, caplog):  # noqa: F811
    short = TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=SOURCE, budget_bytes=tunnel_budget(SOURCE),
                        wall_s=0.3, container=WINDOWS.container, duration_known=True, windows=WINDOWS.windows)
    r = await rig(_two_windows, collect_margin_s=0.2)
    client = await r.call("media.audio_windows", body=start_frame(short))
    frames = [await client.next_frame() for _ in range(3)]
    assert frames[-1].kind is FrameType.RESULT

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(client.ended.wait(), GUARD_S)     # bounded: 0.3 s wall + 0.2 s margin
    assert not client.reset.is_set()
    assert any("did not end its body after RESULT" in rec.getMessage() for rec in caplog.records)
