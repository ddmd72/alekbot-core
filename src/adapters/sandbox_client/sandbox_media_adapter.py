"""
SandboxMediaAdapter: `MediaAudioPort` over the light sandbox's media tunnel (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md
§7 S5; S5 design §2.3 "Deadlines", "Errors", "Result mapping", §2.6).

The recording is decoded in the sandbox, which never holds its URL or a token: the adapter learns the size from one
upstream range, sends a START with sizes and the plan only, and answers the sandbox's OPENs from the drive itself
(`HttpRangeSource`). The windows come back with the verdict and are written as `out_dir/window_<index>.opus`.

Deadlines. The port's wall (`wall_s`; a probe: `PROBE_WALL_S`) is the call's hard end, retries included; START's
`wall_s` is the time left minus `COLLECT_MARGIN_S` (half the time left below twice the margin, never in production),
which leaves the sandbox time to stream the windows back.

Failures:
    a stream lost once (TunnelLost)      one retry when `TRANSCRIBE_MIN_RUN_WALL_S` (a probe: `PROBE_MIN_RUN_WALL_S`)
                                         is left, else ParserUnavailable (uncharged: the tick ends)
    lost twice                           `lost` -> MediaUnreadable("sandbox_lost") (charged); a 503 `recycling` ->
                                         ParserUnavailable("busy"); a 503 the sandbox never sent -> "unavailable"
    TunnelViolation                      MediaUnreadable("tunnel_violation") (charged: only the file steers a decoder
                                         there), WARNING
    SandboxUnavailable / Misconfigured   ParserUnavailable(busy | unavailable | misconfigured), never charged
RESULT mapping: `ok` -> the media inside (a windows run OK, or TIMEOUT with the windows finished before the wall);
`timeout` -> TIMEOUT, no windows; `memory` -> MEMORY; `error`, `too_large` -> MediaUnreadable(detail); `bad_request` ->
ParserUnavailable("misconfigured"), ERROR. One log line per port call: request id, op, status, attempts, opens, bytes,
wall; never the source.

The same adapter serves the main service's `VideoDurationPort` (S5b): the video's bytes are streamed from memory
(`BytesRangeSource`) to the `media.probe` op, and anything but a known duration is `None`.

Public interface:
    SandboxMediaAdapter(client, *, range_source=HttpRangeSource, probe_wall_s=PROBE_WALL_S)
        .probe(source) -> MediaProbeOutcome
        .duration_s(video) -> Optional[float]       never raises (VideoDurationPort)
        .extract_windows(source, windows, out_dir, *, wall_s, container, duration_known=True) -> MediaWindowsOutcome
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
from typing import Callable, List, Optional, Sequence, Tuple

from ...domain.drive_extraction import ParserUnavailable
from ...domain.drive_index_params import MEDIA_PASS_MAX_WINDOWS, TRANSCRIBE_MIN_RUN_WALL_S
from ...domain.drive_media_source import MediaSource
from ...domain.drive_transcript import (
    MediaProbe,
    MediaProbeOutcome,
    MediaStatus,
    MediaUnreadable,
    MediaWindowsOutcome,
    WindowSpec,
)
from ...domain.sandbox_contract import SandboxOp, SandboxStatus
from ...domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from ...domain.sandbox_tunnel import (
    COLLECT_MARGIN_S,
    CONTAINER_MAX_CHARS,
    MEDIA_CEILING,
    MEDIA_MAX_WINDOWS,
    TunnelProbe,
    TunnelResult,
    TunnelStart,
    TunnelViolation,
    TunnelWindow,
    TunnelWindows,
    tunnel_budget,
)
from ...ports.media_audio_port import MediaAudioPort
from ...ports.video_duration_port import VideoDurationPort
from ...utils.logger import logger
from .bytes_range_source import BytesRangeSource
from .media_range_source import HttpRangeSource, RangeSource
from .sandbox_tunnel_client import SandboxTunnelClient, TunnelLost, TunnelReply

PROBE_WALL_S = 60.0                 # the in-process extractor's probe wall
VIDEO_DURATION_WALL_S = 20.0        # a user turn waits on it: the old in-process wall was 15 s, plus a sandbox cold start
PROBE_MIN_RUN_WALL_S = 30.0         # a probe retries only with this much of its wall left
MAX_ATTEMPTS = 2                    # never more than one retry per call
_JITTER_S = (0.05, 0.25)            # before retrying a 503 (another instance may take it)
_CONTAINER = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")     # the probed format, as the codec checks it
_SYSTEMIC_REASON = {"recycling": "busy", "no_answer": "unavailable", "lost": "unavailable"}

RangeSourceFactory = Callable[[MediaSource, float], RangeSource]


def _http_range_source(source: MediaSource, wall_s: float) -> RangeSource:
    return HttpRangeSource(source, wall_s=wall_s)


class SandboxMediaAdapter(MediaAudioPort, VideoDurationPort):
    def __init__(self, client: SandboxTunnelClient, *, range_source: RangeSourceFactory = _http_range_source,
                 probe_wall_s: float = PROBE_WALL_S) -> None:
        self._client = client
        self._range_source = range_source
        self._probe_wall_s = probe_wall_s

    # -- port -------------------------------------------------------------------------------------------------------

    async def probe(self, source: MediaSource) -> MediaProbeOutcome:
        return await self._probe(lambda: self._range_source(source, self._probe_wall_s), self._probe_wall_s)

    async def duration_s(self, video: bytes) -> Optional[float]:
        if not video:
            return None
        try:
            outcome = await self._probe(lambda: BytesRangeSource(video), VIDEO_DURATION_WALL_S)
        except asyncio.CancelledError:
            raise
        except ParserUnavailable as exc:
            logger.warning("[SandboxMedia] video duration unknown: unavailable (%s)", exc)
            return None
        except MediaUnreadable as exc:
            logger.warning("[SandboxMedia] video duration unknown: unreadable (%s)", exc.detail)
            return None
        except Exception as exc:
            logger.warning("[SandboxMedia] video duration unknown: %s", type(exc).__name__)
            return None
        if outcome.status is MediaStatus.OK and outcome.probe is not None and outcome.probe.duration_known:
            return outcome.probe.duration_s
        logger.warning("[SandboxMedia] video duration unknown: %s", outcome.status.value)
        return None

    async def _probe(self, open_upstream: Callable[[], RangeSource], wall_s: float) -> MediaProbeOutcome:
        def make(size: int, budget: int, child_wall: float) -> TunnelStart:
            return TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=size, budget_bytes=budget, wall_s=child_wall)

        reply = await self._call(SandboxOp.MEDIA_PROBE, open_upstream, wall_s, PROBE_MIN_RUN_WALL_S, make)
        result = reply.result
        if result.status is SandboxStatus.OK and isinstance(result.media, TunnelProbe):
            media = result.media
            return MediaProbeOutcome(status=MediaStatus.OK, probe=MediaProbe(
                duration_s=media.duration_s, has_audio=media.has_audio, container=media.container,
                duration_known=media.duration_known))
        status, detail = self._run_failure(SandboxOp.MEDIA_PROBE, result)
        return MediaProbeOutcome(status=status, detail=detail or "probe wall")

    async def extract_windows(self, source: MediaSource, windows: Sequence[WindowSpec], out_dir: str, *, wall_s: float,
                              container: str, duration_known: bool = True) -> MediaWindowsOutcome:
        if not container or len(container) > CONTAINER_MAX_CHARS or not _CONTAINER.fullmatch(container):
            raise MediaUnreadable("container missing or invalid")
        if not windows:
            return MediaWindowsOutcome(status=MediaStatus.OK)
        indexes = [w.index for w in windows]
        if not duration_known and (len(windows) > MEDIA_PASS_MAX_WINDOWS
                                   or indexes != list(range(indexes[0], indexes[0] + len(windows)))):
            raise ValueError("a pass takes a contiguous slice of at most MEDIA_PASS_MAX_WINDOWS windows")
        if len(windows) > MEDIA_MAX_WINDOWS or len(set(indexes)) != len(indexes):
            raise ValueError("a run takes at most MEDIA_MAX_WINDOWS distinct windows")
        try:
            plan = tuple(TunnelWindow(index=w.index, start_s=float(w.start_s), duration_s=float(w.duration_s))
                         for w in windows)
        except TunnelViolation as exc:
            raise ValueError(f"a window of the plan is out of range: {exc}") from None
        if wall_s <= 0:
            logger.warning("[SandboxMedia] extract_windows: no wall left, no request sent")
            return MediaWindowsOutcome(status=MediaStatus.TIMEOUT, detail="deadline")

        def make(size: int, budget: int, child_wall: float) -> TunnelStart:
            return TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=size, budget_bytes=budget,
                               wall_s=child_wall, container=container, duration_known=duration_known, windows=plan)

        reply = await self._call(SandboxOp.MEDIA_AUDIO_WINDOWS, lambda: self._range_source(source, wall_s), wall_s,
                                 float(TRANSCRIBE_MIN_RUN_WALL_S), make)
        result = reply.result
        if result.status is not SandboxStatus.OK or not isinstance(result.media, TunnelWindows):
            status, detail = self._run_failure(SandboxOp.MEDIA_AUDIO_WINDOWS, result)
            return MediaWindowsOutcome(status=status, detail=detail)
        media = result.media
        written = await self._write_windows(reply, out_dir)
        specs = tuple((WindowSpec(index=w.index, start_s=w.start_s, duration_s=w.duration_s), path)
                      for w, path in zip(media.windows, written))
        return MediaWindowsOutcome(status=MediaStatus(media.status.value), windows=specs, duration_s=media.duration_s,
                                   reached_bound=media.reached_bound, eof=media.eof, detail=media.detail)

    # -- the call ---------------------------------------------------------------------------------------------------

    async def _call(self, op: SandboxOp, open_upstream: Callable[[], RangeSource], wall_s: float, min_run_s: float,
                    make: Callable[[int, int, float], TunnelStart]) -> TunnelReply:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + wall_s
        log = {"request_id": "-", "status": "error", "attempts": 0, "opens": 0, "bytes_in": 0}
        try:
            upstream = open_upstream()
            try:
                try:
                    async with asyncio.timeout_at(deadline):
                        size = await upstream.size()
                except TimeoutError:
                    raise MediaUnreadable("upstream size timeout") from None
                budget = tunnel_budget(size)
                while True:
                    left = deadline - loop.time()
                    child_wall = min(MEDIA_CEILING.wall_s, max(left - COLLECT_MARGIN_S, left / 2))
                    if child_wall <= 0:
                        raise ParserUnavailable("unavailable")
                    log["attempts"] += 1
                    try:
                        reply = await self._client.run(op, make(size, budget, child_wall), upstream.stream, deadline)
                    except TunnelLost as lost:
                        if log["attempts"] >= MAX_ATTEMPTS:
                            if lost.kind == "lost":
                                logger.warning("[SandboxMedia] op=%s the sandbox was lost twice", op.value)
                                raise MediaUnreadable("sandbox_lost") from None
                            raise ParserUnavailable(_SYSTEMIC_REASON[lost.kind]) from None
                        if deadline - loop.time() < min_run_s:
                            logger.warning("[SandboxMedia] op=%s lost with no time left for a retry", op.value)
                            raise ParserUnavailable(_SYSTEMIC_REASON[lost.kind]) from None
                        if lost.kind != "lost":
                            await asyncio.sleep(random.uniform(*_JITTER_S))
                        continue
                    log.update(request_id=reply.request_id, status=reply.result.status.value, opens=reply.opens,
                               bytes_in=reply.bytes_in)
                    return reply
            finally:
                await upstream.aclose()
        except TunnelViolation:
            log["status"] = "tunnel_violation"
            raise MediaUnreadable("tunnel_violation") from None
        except SandboxUnavailable as exc:
            log["status"] = "unavailable"
            raise ParserUnavailable(exc.reason) from None
        except SandboxMisconfigured:
            log["status"] = "misconfigured"
            raise ParserUnavailable("misconfigured") from None
        except ParserUnavailable as exc:
            log["status"] = f"parser_unavailable:{exc}"
            raise
        except MediaUnreadable as exc:
            log["status"] = f"unreadable:{exc.detail}"
            raise
        finally:
            line = logger.info if log["status"] == SandboxStatus.OK.value else logger.warning
            line("[SandboxMedia] request_id=%s op=%s status=%s attempts=%d opens=%d bytes_in=%d wall_s=%.3f",
                 log["request_id"], op.value, log["status"], log["attempts"], log["opens"], log["bytes_in"],
                 loop.time() - started)

    @staticmethod
    def _run_failure(op: SandboxOp, result: TunnelResult) -> Tuple[MediaStatus, str]:
        """A RESULT other than `ok`: TIMEOUT / MEMORY as outcomes, the rest raised."""
        if result.status is SandboxStatus.TIMEOUT:
            return MediaStatus.TIMEOUT, result.detail
        if result.status is SandboxStatus.MEMORY:
            return MediaStatus.MEMORY, result.detail
        if result.status is SandboxStatus.BAD_REQUEST:
            logger.error("[SandboxMedia] op=%s the sandbox refused our START: %s", op.value, result.detail)
            raise ParserUnavailable("misconfigured")
        raise MediaUnreadable(result.detail or result.status.value)

    @staticmethod
    async def _write_windows(reply: TunnelReply, out_dir: str) -> List[str]:
        """Every window of the reply at `out_dir/window_<index>.opus`; all or nothing."""
        written: List[str] = []
        try:
            await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)
            for index, data in reply.windows:
                path = os.path.join(out_dir, f"window_{index}.opus")
                written.append(path)
                await asyncio.to_thread(_write, path, data)
        except BaseException:
            logger.warning("[SandboxMedia] writing the windows failed; removing them")
            for path in written:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            raise
        return written


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)

