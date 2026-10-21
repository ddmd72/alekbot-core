"""
SubprocessMediaExtractor — audio-only Opus windows of a drive recording, cut by ffmpeg inside the parser child
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4, Δ99, Δ134; plan G14, G15, G35).

The indexer process never decodes a byte of media: every ffprobe/ffmpeg run is a grandchild of a fresh parser child
under RLIMIT_CPU / RLIMIT_FSIZE / RLIMIT_AS, as the unprivileged `DRIVE_PARSER_USER` where one is configured, with a
wall-clock kill of the whole process group. The download URL goes to the child on stdin and is never logged. One child
at a time: it shares the instance with the document parser, and the tick runs one stage at a time. RLIMIT_CPU is per process: each ffmpeg of a step
gets its own 150 s, so a step's total is bounded by the wall clock, not by that limit.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import ssl
import tempfile
import time
from typing import Any, Dict, Optional, Sequence, Tuple

import src.adapters.drive_parser.media_launch as launch
from src.domain.drive_index_params import MEDIA_PASS_MAX_WINDOWS, TRANSCRIBE_CHILD_CPU_S, TRANSCRIBE_MAX_SECONDS
from src.domain.drive_transcript import (
    MediaProbe, MediaProbeOutcome, MediaStatus, MediaUnreadable, MediaWindowsOutcome, WindowSpec,
)
from src.ports.media_audio_port import MediaAudioPort
from src.utils.logger import logger

MEDIA_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024    # RLIMIT_AS (the spike: ffmpeg needed 19 MB)
MEDIA_FILE_LIMIT_BYTES = 64 * 1024 * 1024        # RLIMIT_FSIZE: one Opus window (30 min at 24 kb/s is ~5 MB)
PROBE_WALL_S = 60.0
CHILD_WALL_MARGIN_S = 10.0                       # the child stops its own ffmpeg this long before the parent's kill
_FALLBACK_CA_FILE = "/etc/ssl/certs/ca-certificates.crt"
_MAX_WINDOW_INDEX = 1_000
_CONTAINER = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")


def system_ca_file() -> str:
    return ssl.get_default_verify_paths().cafile or _FALLBACK_CA_FILE


class SubprocessMediaExtractor(MediaAudioPort):
    def __init__(self, *, memory_limit_bytes: int = MEDIA_MEMORY_LIMIT_BYTES, cpu_limit_s: int = TRANSCRIBE_CHILD_CPU_S,
                 file_limit_bytes: int = MEDIA_FILE_LIMIT_BYTES, probe_wall_s: float = PROBE_WALL_S,
                 ca_file: Optional[str] = None, python: Optional[str] = None, child_module: str = launch.CHILD_MODULE,
                 temp_root: Optional[str] = None, sandbox_user: Optional[str] = None) -> None:
        self._sandbox = launch.resolve_sandbox(sandbox_user)
        self._memory = memory_limit_bytes
        self._cpu = cpu_limit_s
        self._file_bytes = file_limit_bytes
        self._probe_wall = probe_wall_s
        self._ca_file = ca_file or system_ca_file()
        self._python = python or launch.default_python()
        self._module = child_module
        self._temp_root = temp_root
        self._slots = asyncio.Semaphore(1)

    # -- port -------------------------------------------------------------------------------------------------------

    async def probe(self, url: str) -> MediaProbeOutcome:
        result, status, detail = await self._run({"op": "media_probe", "url": url, "ca_file": self._ca_file},
                                                 self._probe_wall)
        if status is not MediaStatus.OK or result is None:
            return MediaProbeOutcome(status=status, detail=detail)
        try:
            duration = float(result["duration_s"])
            known = result["duration_known"]
            audio = result["has_audio"]
            container = result["container"]
            if (not isinstance(known, bool) or not isinstance(audio, bool) or not isinstance(container, str)
                    or not math.isfinite(duration) or duration < 0 or (known and duration <= 0)):
                raise ValueError("bad field")
        except (KeyError, ValueError, TypeError):
            logger.warning("[DriveMedia] probe result has an invalid shape")
            raise MediaUnreadable("invalid probe result") from None
        return MediaProbeOutcome(status=MediaStatus.OK, detail="", probe=MediaProbe(
            duration_s=duration if known else 0.0, has_audio=audio, container=container[:80], duration_known=known))

    async def extract_windows(self, url: str, windows: Sequence[WindowSpec], out_dir: str, *, wall_s: float,
                              container: str, duration_known: bool = True) -> MediaWindowsOutcome:
        if not container or not _CONTAINER.match(container):
            raise MediaUnreadable("container missing or invalid")
        if not windows:
            return MediaWindowsOutcome(status=MediaStatus.OK)
        indexes = [w.index for w in windows]
        if not duration_known and (len(windows) > MEDIA_PASS_MAX_WINDOWS
                                   or indexes != list(range(indexes[0], indexes[0] + len(windows)))):
            raise ValueError("a pass takes a contiguous slice of at most MEDIA_PASS_MAX_WINDOWS windows")
        payload: Dict[str, Any] = {
            "op": "media_windows", "url": url, "ca_file": self._ca_file, "container": container,
            "duration_known": duration_known,
            "windows": [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s} for w in windows]}
        allowed = set(indexes)
        os.makedirs(out_dir, exist_ok=True)
        result, status, detail = await self._run(payload, wall_s, out_dir=out_dir)
        if result is None:
            return MediaWindowsOutcome(status=status, detail=detail)
        specs = []
        for item in result["_collected"]:
            if item[0] not in allowed:
                raise MediaUnreadable("window outside the request")
            specs.append((WindowSpec(index=item[0], start_s=item[1], duration_s=item[2]), item[3]))
        eof = result.get("eof") is True
        reached = result.get("reached_bound") is True and not duration_known and not eof
        total = 0.0
        if eof:
            total = result.get("duration_s")
            if isinstance(total, bool) or not isinstance(total, (int, float)) or not 0 < total <= float(
                    TRANSCRIBE_MAX_SECONDS) + 1:
                raise MediaUnreadable("invalid duration")
        return MediaWindowsOutcome(status=status, windows=tuple(specs), duration_s=float(total),
                                   reached_bound=reached, eof=eof, detail=detail)

    # -- child ------------------------------------------------------------------------------------------------------

    async def _run(self, payload: Dict[str, Any], wall_s: float, *, out_dir: Optional[str] = None
                   ) -> Tuple[Optional[Dict[str, Any]], MediaStatus, str]:
        """(result, OK, "") on a result; (None, TIMEOUT|MEMORY, detail) on a kill; raises MediaUnreadable otherwise. A
        `windows` result carries `_collected`: (index, start_s, duration_s, final path) per window copied to out_dir."""
        async with self._slots:
            workdir = tempfile.mkdtemp(prefix="drive-media-", dir=self._temp_root)
            started = time.monotonic()
            op = str(payload.get("op"))
            try:
                if self._sandbox is not None:
                    os.chown(workdir, *self._sandbox)       # mode 0700 from mkdtemp: the child's alone
                out = os.path.join(workdir, "result.json")
                child_wall = max(wall_s - CHILD_WALL_MARGIN_S, wall_s * 0.5)
                request = {**payload, "out": out, "memory_bytes": self._memory, "cpu_s": self._cpu,
                           "file_bytes": self._file_bytes, "wall_s": child_wall, "out_dir": workdir}
                if len(json.dumps(request)) > launch.MAX_REQUEST_BYTES:
                    raise MediaUnreadable("request too large")
                with open(os.path.join(workdir, "stderr.txt"), "w+b") as errors:
                    code, timed_out = await launch.spawn(request, workdir, errors, python=self._python,
                                                         module=self._module, wall_s=wall_s, sandbox=self._sandbox)
                    kind, detail = launch.exit_status(code, timed_out, wall_s, self._cpu)
                    digest = launch.stderr_digest(errors) if kind == "error" else ""
                elapsed = time.monotonic() - started
                if kind in ("timeout", "memory"):
                    logger.warning(f"[DriveMedia] {op}: {kind} ({detail}) after {elapsed:.1f}s")
                    return None, MediaStatus(kind), detail
                if kind == "error":
                    logger.warning(f"[DriveMedia] {op}: {detail}; {digest} after {elapsed:.1f}s")
                    raise MediaUnreadable(f"{detail}; {digest}")
                result = launch.load_result(out)
                if result is None:
                    logger.warning(f"[DriveMedia] {op}: no valid result after {elapsed:.1f}s")
                    raise MediaUnreadable("no valid result")
                child_status = result.get("status")
                if child_status == "error":
                    logger.warning(f"[DriveMedia] {op}: child reported an error after {elapsed:.1f}s")
                    raise MediaUnreadable(str(result.get("detail", ""))[:100])
                if child_status not in ("ok", "timeout"):
                    raise MediaUnreadable("invalid result")
                status = MediaStatus.OK if child_status == "ok" else MediaStatus.TIMEOUT
                if out_dir is not None:
                    result["_collected"] = self._collect(result, workdir, out_dir, payload)
                    if status is MediaStatus.TIMEOUT:
                        logger.warning(f"[DriveMedia] {op}: wall after {len(result['_collected'])} windows, "
                                       f"{elapsed:.1f}s")
                elif status is MediaStatus.TIMEOUT:
                    return None, status, "probe wall"
                return result, status, str(result.get("detail", ""))[:100] if status is MediaStatus.TIMEOUT else ""
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    def _collect(self, result: Dict[str, Any], workdir: str, out_dir: str, payload: Dict[str, Any]
                 ) -> Tuple[Tuple[int, float, float, str], ...]:
        """The windows the child reports, copied out of its directory. The child is not trusted further than its
        sandbox: shape, index range and each file (regular, within the size limit) are checked."""
        items = result.get("windows")
        if not isinstance(items, list) or len(items) > _MAX_WINDOW_INDEX:
            raise MediaUnreadable("invalid windows")
        collected = []
        seen = set()
        for item in items:
            try:
                index, start, length = item["index"], float(item["start_s"]), float(item["duration_s"])
                if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < _MAX_WINDOW_INDEX
                        or index in seen or not math.isfinite(start) or not math.isfinite(length)
                        or start < 0 or length <= 0):
                    raise ValueError("bad window")
            except (KeyError, TypeError, ValueError):
                logger.warning("[DriveMedia] child window entry has an invalid shape")
                raise MediaUnreadable("invalid window entry") from None
            seen.add(index)
            name = f"window_{index}.opus"
            wanted = os.path.join(out_dir, name)
            if not launch.collect_window(os.path.join(workdir, name), wanted, self._file_bytes):
                logger.warning("[DriveMedia] a reported window is not a regular file within the limit")
                for _, _, _, path in collected:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
                raise MediaUnreadable("invalid window file")
            collected.append((index, start, length, wanted))
        return tuple(collected)
