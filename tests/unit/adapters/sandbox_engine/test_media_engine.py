"""MediaEngine (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S5; S5 design §2.3-§2.4) with a scripted child (media_child_stub.py)
started through the real launcher: the tunnel's bounds kill the child and end the run `error/tunnel_bounds` whichever
side breaks them, the child's files are read through the parse engine's discipline, and the RESULT is the child's
result through the domain codec. The real child with real ffmpeg is in test_media_engine_ffmpeg.py."""
import asyncio
import json
import os
import sys
import time

import pytest

from src.adapters.sandbox_engine import media_engine
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.media_engine import MediaEngine
from src.domain.sandbox_contract import SandboxLimits, SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    DATA_MAX, END_EOF, OPEN_SLACK_BYTES, Frame, FrameType, SandboxTunnelEngine, TunnelProbe, TunnelStart,
    TunnelViolation, TunnelWindow, TunnelWindows, tunnel_budget,
)

pytestmark = pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                                reason="children run as the invoking user: needs a non-root user")

_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_child_stub.py")
_SIZE = 10_000
_LIMITS = SandboxLimits(wall_s=20.0, memory_bytes=1 << 30, cpu_s=20.0, file_bytes=64 * 1024 * 1024)


def _probe_start(budget=None, size=_SIZE):
    return TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=size, budget_bytes=budget or tunnel_budget(size),
                       wall_s=20.0)


def _windows_start(count=2):
    return TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=_SIZE, budget_bytes=tunnel_budget(_SIZE),
                       wall_s=20.0, container="mov,mp4,m4a,3gp,3g2,mj2",
                       windows=tuple(TunnelWindow(index=i + 4, start_s=i * 1770.0, duration_s=1800.0)
                                     for i in range(count)))


class Rig:
    def __init__(self, tmp_path, behaviour, op=SandboxOp.MEDIA_PROBE, **engine_kw):
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        (self.workdir / "behaviour.json").write_text(json.dumps(behaviour))
        self.out_root = tmp_path / "out"
        self.out_root.mkdir(mode=0o700)
        self.engine = MediaEngine(ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(self.out_root),
                                  child_script=_STUB, **engine_kw)

    async def start(self, start, limits=_LIMITS):
        return await self.engine.start(str(self.workdir), limits, start)

    def seen_request(self):
        return json.loads((self.workdir / "seen_request.json").read_text())


async def _drain(link):
    """The route's record pump: every OPEN / CLOSE until the child's side ends."""
    frames = []
    while (frame := await link.receive()) is not None:
        frames.append(frame)
    return frames


def test_the_engine_is_a_sandbox_tunnel_engine(tmp_path):
    engine = MediaEngine(ChildLauncher(), op=SandboxOp.MEDIA_PROBE, output_root=None)
    assert isinstance(engine, SandboxTunnelEngine)
    with pytest.raises(ValueError):
        MediaEngine(ChildLauncher(), op=SandboxOp.PARSE_EXTRACT)


async def test_a_start_for_another_op_is_refused(tmp_path):
    rig = Rig(tmp_path, "ok", op=SandboxOp.MEDIA_AUDIO_WINDOWS)
    with pytest.raises(ValueError):
        await rig.start(_probe_start())


async def test_a_probe_answers_the_childs_result(tmp_path):
    rig = Rig(tmp_path, "ok")
    run = await rig.start(_probe_start())
    frames, done = await asyncio.gather(_drain(run.link), run.wait())
    assert frames == []
    assert done.result.status is SandboxStatus.OK
    assert done.result.media == TunnelProbe(duration_s=12.5, duration_known=True, has_audio=True,
                                            container="mov,mp4,m4a,3gp,3g2,mj2")
    assert done.windows == () and done.output_dir is None
    assert done.opens == 0 and done.bytes_in == 0


async def test_the_child_request_holds_the_limits_and_the_child_wall_and_no_url(tmp_path):
    rig = Rig(tmp_path, "ok", op=SandboxOp.MEDIA_AUDIO_WINDOWS)
    run = await rig.start(_windows_start())
    await run.wait()
    seen = rig.seen_request()
    assert seen["op"] == "media_windows" and seen["source_bytes"] == _SIZE
    assert seen["wall_s"] == 10.0                    # max(20 - 10, 20 / 2): the child stops before the launcher kills
    assert seen["memory_bytes"] == 1 << 30 and seen["cpu_s"] == 20 and seen["file_bytes"] == 64 * 1024 * 1024
    assert seen["container"] == "mov,mp4,m4a,3gp,3g2,mj2" and seen["duration_known"] is True
    assert [w["index"] for w in seen["windows"]] == [4, 5]
    assert "url" not in seen and "ca_file" not in seen
    assert seen["out_dir"] == str(rig.workdir)


async def test_the_link_moves_open_data_end_close_between_caller_and_child(tmp_path):
    rig = Rig(tmp_path, "echo")
    run = await rig.start(_probe_start())
    opened = await run.link.receive()
    assert opened == Frame(FrameType.OPEN, 1, 2)
    await run.link.send(Frame(FrameType.DATA, 1, 2, b"Alex's plot"))
    await run.link.send(Frame(FrameType.DATA, 1, 13, b" and Kim's"))
    await run.link.send(Frame(FrameType.END, 1, END_EOF))
    closed = await run.link.receive()
    assert closed == Frame(FrameType.CLOSE, 1)
    done = await run.wait()
    assert done.result.status is SandboxStatus.OK and done.result.media.has_audio is True   # END value 0 seen
    assert (rig.workdir / "echo.bin").read_bytes() == b"Alex's plot and Kim's"
    assert done.opens == 1 and done.bytes_in == 21


async def test_windows_are_copied_out_of_the_workdir_in_order(tmp_path):
    rig = Rig(tmp_path, "ok", op=SandboxOp.MEDIA_AUDIO_WINDOWS)
    run = await rig.start(_windows_start())
    done = await run.wait()
    assert done.result.status is SandboxStatus.OK
    assert [w.index for w in done.result.media.windows] == [4, 5]
    assert [index for index, _ in done.windows] == [4, 5]
    for index, path in done.windows:
        assert os.path.dirname(path) == done.output_dir
        assert os.path.dirname(done.output_dir) == str(rig.out_root)
        with open(path, "rb") as fh:
            assert fh.read() == b"OggS-window-%d" % index


@pytest.mark.parametrize("behaviour", ["open_beyond", "many_opens", "three_open", "bad_record"])
async def test_a_child_that_breaks_a_bound_is_killed_and_the_run_is_tunnel_bounds(tmp_path, behaviour):
    rig = Rig(tmp_path, behaviour)
    run = await rig.start(_probe_start())
    started = time.monotonic()
    frames, done = await asyncio.gather(_drain(run.link), run.wait())
    assert time.monotonic() - started < 10          # killed, not left to the 20 s wall
    assert done.result.status is SandboxStatus.ERROR and done.result.detail == "tunnel_bounds"
    assert done.result.media is None and done.windows == ()
    if behaviour == "many_opens":
        assert done.opens == 256 and len(frames) == 512


async def test_caller_bytes_over_the_budget_kill_the_child(tmp_path):
    # Δ13 amended (14e701f7): the allowance is the budget plus OPEN_SLACK_BYTES per OPEN, so the caller must stream
    # past 1 000 + 8 MiB on the one stream the child opened (the echo child drains them) before one more byte breaks it.
    allowed = 1_000 + OPEN_SLACK_BYTES
    rig = Rig(tmp_path, "echo")
    run = await rig.start(_probe_start(budget=1_000, size=allowed + DATA_MAX))
    assert await run.link.receive() == Frame(FrameType.OPEN, 1, 2)
    offset, sent = 2, 0
    while sent < allowed:
        chunk = min(DATA_MAX, allowed - sent)
        await run.link.send(Frame(FrameType.DATA, 1, offset, b"g" * chunk))
        offset, sent = offset + chunk, sent + chunk
    with pytest.raises(TunnelViolation):
        await run.link.send(Frame(FrameType.DATA, 1, offset, b"g"))
    done = await run.wait()
    assert done.result.status is SandboxStatus.ERROR and done.result.detail == "tunnel_bounds"
    assert done.bytes_in == allowed + 1
    await run.link.send(Frame(FrameType.DATA, 1, offset + 1, b"g"))      # after the end: dropped, not raised
    assert await run.link.receive() is None


@pytest.mark.parametrize("frame", [
    Frame(FrameType.DATA, 1, 5, b"out of order"),
    Frame(FrameType.DATA, 2, 0, b"never opened"),
    Frame(FrameType.OPEN, 1, 0),
])
async def test_a_caller_frame_outside_the_contract_kills_the_child(tmp_path, frame):
    rig = Rig(tmp_path, "open_and_wait")
    run = await rig.start(_probe_start())
    assert await run.link.receive() == Frame(FrameType.OPEN, 1, 0)
    with pytest.raises(TunnelViolation):
        await run.link.send(frame)
    done = await run.wait()
    assert done.result.detail == "tunnel_bounds"


@pytest.mark.slow
async def test_the_wall_clock_kill_is_a_timeout(tmp_path):
    rig = Rig(tmp_path, "sleep")
    limits = SandboxLimits(wall_s=1.0, memory_bytes=1 << 30, cpu_s=20.0, file_bytes=64 * 1024 * 1024)
    run = await rig.start(_probe_start(), limits=limits)
    done = await run.wait()
    assert done.result.status is SandboxStatus.TIMEOUT and done.result.detail == "wall clock 1s"


@pytest.mark.parametrize("behaviour, status, detail", [
    ("exit3", SandboxStatus.MEMORY, "MemoryError"),
    ("no_result", SandboxStatus.ERROR, "no valid result"),
    ("child_error", SandboxStatus.ERROR, "ffmpeg exit 183"),
])
async def test_child_failures_map_to_a_typed_result(tmp_path, behaviour, status, detail):
    rig = Rig(tmp_path, behaviour)
    done = await (await rig.start(_probe_start())).wait()
    assert (done.result.status, done.result.detail) == (status, detail)


@pytest.mark.parametrize("behaviour", ["window_symlink", "window_fifo", "window_hardlink", "window_big",
                                       "window_missing"])
async def test_a_window_that_is_not_a_plain_file_within_the_cap_fails_the_run(tmp_path, behaviour):
    rig = Rig(tmp_path, behaviour, op=SandboxOp.MEDIA_AUDIO_WINDOWS, window_max_bytes=1_000)
    done = await (await rig.start(_windows_start())).wait()
    assert done.result.status is SandboxStatus.ERROR and done.result.detail == "invalid window file"
    assert done.windows == () and done.output_dir is None
    assert os.listdir(rig.out_root) == []


async def test_windows_past_the_response_cap_are_left_out_as_a_timeout(tmp_path):
    cap = media_engine.FRAME_HEADER_BYTES + media_engine.RESULT_MAX_BYTES + 2 * (17 + 400) + 100
    rig = Rig(tmp_path, "windows_400", op=SandboxOp.MEDIA_AUDIO_WINDOWS, response_cap_bytes=cap)
    done = await (await rig.start(_windows_start(count=3))).wait()
    media = done.result.media
    assert done.result.status is SandboxStatus.OK and isinstance(media, TunnelWindows)
    assert media.status is SandboxStatus.TIMEOUT and media.detail == "response cap"
    assert [w.index for w in media.windows] == [4, 5] and media.eof is False and media.duration_s == 0
    assert [index for index, _ in done.windows] == [4, 5]


async def test_cancelling_wait_kills_the_child(tmp_path):
    rig = Rig(tmp_path, "sleep")
    run = await rig.start(_probe_start())
    waiter = asyncio.ensure_future(run.wait())
    await asyncio.sleep(0.3)
    started = time.monotonic()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert time.monotonic() - started < 5
    assert await run.link.receive() is None


def test_the_exit_codes_mirror_the_drive_child():
    from src.adapters.drive_parser import child
    assert (media_engine.EXIT_MEMORY, media_engine.EXIT_BAD_REQUEST) == (child.EXIT_MEMORY, child.EXIT_BAD_REQUEST)


def test_the_window_name_mirrors_the_drive_media_module():
    from src.adapters.drive_parser import media
    assert media_engine.window_file(7) == media.window_name(7)
