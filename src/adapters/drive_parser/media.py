"""
Child-side ffprobe / ffmpeg for the drive transcript extractor (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4, Δ99, Δ134).

Runs inside the unprivileged parser child after `serve` applied the limits (ffmpeg inherits them). The URL arrives in the
stdin request and lives in the ffmpeg argv only: readable by root and the parser uid, the named Δ99 secret. Nothing here
logs it, and ffmpeg's stderr is left to the parent's digest.

Public interface:
    build_probe_argv(url, ca_file) -> List[str]
    build_window_argv(url, ca_file, container, start_s, length_s, out_path) -> List[str]
    build_segment_argv(url, ca_file, container, start_s, total_s, segment_s, out_pattern) -> List[str]
    run_probe(request) -> dict
    run_windows(request) -> dict
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from src.domain.drive_index_params import TRANSCRIBE_MAX_SECONDS, TRANSCRIBE_WINDOW_S

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
PROTOCOL_WHITELIST = "https,tls,tcp,crypto"
# G14: a short name admits the demuxer whose name lists it ("mov" -> "mov,mp4,m4a,3gp,3g2,mj2"); copied to nested inputs.
FORMAT_WHITELIST = "mov,mp4,m4a,matroska,webm,mp3,wav,flac,ogg,aac"
NETWORK_TIMEOUT_US = 30_000_000       # -rw_timeout: a stalled socket ends as an error, not a wall-clock kill
MAX_WINDOWS_PER_RUN = 64
_FORMAT_NAME = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")
_BOUND_SLACK_S = 1.0                  # a pass within this of its `-t` bound reached it
_MIN_BUDGET_S = 1.0


def window_name(index: int) -> str:
    return f"window_{index}.opus"


def _input_guard(url: str, ca_file: str, container: str) -> List[str]:
    """Flags common to every run: allowlists, TLS verification (ffmpeg verifies nothing by default), the network
    timeout, and the probed demuxer pinned with `-f` when known (G14)."""
    argv = ["-protocol_whitelist", PROTOCOL_WHITELIST, "-format_whitelist", FORMAT_WHITELIST,
            "-tls_verify", "1", "-ca_file", ca_file, "-rw_timeout", str(NETWORK_TIMEOUT_US)]
    if container:
        if not _FORMAT_NAME.match(container):
            raise ValueError("container name has an invalid shape")
        argv += ["-f", container]
    return argv


def build_probe_argv(url: str, ca_file: str) -> List[str]:
    """ffprobe has no `-nostdin`; the runner gives it a closed stdin instead."""
    return [FFPROBE, "-hide_banner", "-v", "error", "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-format_whitelist", FORMAT_WHITELIST, "-tls_verify", "1", "-ca_file", ca_file,
            "-rw_timeout", str(NETWORK_TIMEOUT_US), "-print_format", "json",
            "-show_entries", "format=duration,format_name:stream=codec_type", "-i", url]


_OUTPUT_FLAGS = ["-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                 "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k"]


def build_window_argv(url: str, ca_file: str, container: str, start_s: float, length_s: float,
                      out_path: str) -> List[str]:
    """One window: input-side `-ss` / `-t` (Range reads, Δ134), audio only, to `out_path`."""
    return ([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
            + _input_guard(url, ca_file, container)
            + ["-ss", f"{start_s:.3f}", "-t", f"{length_s:.3f}", "-i", url] + _OUTPUT_FLAGS + ["-f", "opus", out_path])


def build_segment_argv(url: str, ca_file: str, container: str, start_s: float, total_s: float, segment_s: float,
                       out_pattern: str) -> List[str]:
    """All windows in one pass with the `segment` muxer (Δ134); `-t` bounds the pass. `-ss` only when resuming."""
    seek = ["-ss", f"{start_s:.3f}"] if start_s > 0 else []
    return ([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
            + _input_guard(url, ca_file, container)
            + seek + ["-t", f"{total_s:.3f}", "-i", url] + _OUTPUT_FLAGS
            + ["-f", "segment", "-segment_time", f"{segment_s:.3f}", "-segment_format", "opus",
               "-reset_timestamps", "1", out_pattern])


# -- runners ---------------------------------------------------------------------------------------------------------

def run_probe(request: Dict[str, Any]) -> Dict[str, Any]:
    argv = build_probe_argv(str(request["url"]), str(request["ca_file"]))
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=None,
                              timeout=float(request["wall_s"]) - 2.0, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": "probe wall"}
    if done.returncode != 0:
        return {"status": "error", "detail": f"ffprobe exit {done.returncode}"}
    try:
        info = json.loads(done.stdout.decode("utf-8", "replace"))
        fmt = info.get("format") or {}
        container = str(fmt.get("format_name", ""))
        streams = info.get("streams") or []
        has_audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
        raw = fmt.get("duration")
        duration = float(raw) if raw not in (None, "", "N/A") else 0.0
    except (ValueError, TypeError, AttributeError):
        return {"status": "error", "detail": "ffprobe output is not valid"}
    known = math.isfinite(duration) and duration > 0
    return {"status": "ok", "duration_s": duration if known else 0.0, "duration_known": known,
            "has_audio": has_audio, "container": container if _FORMAT_NAME.match(container) else ""}


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _run_ffmpeg(argv: List[str], deadline: float) -> Tuple[Optional[int], bool]:
    """(return code, timed_out). Stdin closed, stderr inherited (the parent's capped file), stdout discarded."""
    budget = max(_remaining(deadline), _MIN_BUDGET_S)
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=budget, check=False)
    except subprocess.TimeoutExpired:
        return None, True
    return done.returncode, False


def _regular(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _file_duration(path: str) -> float:
    """Duration of a window we wrote ourselves (local Ogg Opus file, `file` only)."""
    argv = [FFPROBE, "-hide_banner", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", "ogg",
            "-show_entries", "format=duration", "-of", "csv=p=0", "-i", path]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, timeout=30, check=False)
        value = float(done.stdout.decode("ascii", "replace").strip())
    except (subprocess.TimeoutExpired, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def run_windows(request: Dict[str, Any]) -> Dict[str, Any]:
    url, ca_file = str(request["url"]), str(request["ca_file"])
    container = str(request.get("container", ""))
    out_dir = str(request["out_dir"])
    deadline = time.monotonic() + float(request["wall_s"])
    try:
        if not request.get("duration_known", True) or request.get("one_pass"):
            return _run_segment(request, url, ca_file, container, out_dir, deadline)
        return _run_each(request, url, ca_file, container, out_dir, deadline)
    except ValueError as exc:                  # a bad container name or request shape: the file's, not a crash
        return {"status": "error", "detail": str(exc)[:100]}


def _run_each(request: Dict[str, Any], url: str, ca_file: str, container: str, out_dir: str,
              deadline: float) -> Dict[str, Any]:
    specs = request.get("windows") or []
    if not isinstance(specs, list) or not 0 < len(specs) <= MAX_WINDOWS_PER_RUN:
        raise ValueError("window count out of range")
    written: List[Dict[str, Any]] = []
    for spec in specs:
        index, start, length = int(spec["index"]), float(spec["start_s"]), float(spec["duration_s"])
        if index < 0 or not (math.isfinite(start) and math.isfinite(length)) or start < 0 or length <= 0:
            raise ValueError("window spec out of range")
        if _remaining(deadline) <= _MIN_BUDGET_S:
            return {"status": "timeout", "windows": written, "detail": "wall before window"}
        final = os.path.join(out_dir, window_name(index))
        part = final + ".part"
        code, timed_out = _run_ffmpeg(build_window_argv(url, ca_file, container, start, length, part), deadline)
        if timed_out:
            _discard(part)
            return {"status": "timeout", "windows": written, "detail": "wall in window"}
        if code != 0 or not _regular(part) or os.path.getsize(part) == 0:
            _discard(part)
            return {"status": "error", "detail": f"ffmpeg exit {code}"}
        os.replace(part, final)
        written.append({"index": index, "start_s": start, "duration_s": length})
    return {"status": "ok", "windows": written}


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _run_segment(request: Dict[str, Any], url: str, ca_file: str, container: str, out_dir: str,
                 deadline: float) -> Dict[str, Any]:
    """One pass with the `segment` muxer. Unknown duration (G35): from the start, `-t TRANSCRIBE_MAX_SECONDS`, windows
    of TRANSCRIBE_WINDOW_S; the covered time is read from the produced files. Known: the planned windows' span."""
    if not request.get("duration_known", True):
        first_index, start_s, total_s, segment_s = 0, 0.0, float(TRANSCRIBE_MAX_SECONDS), float(TRANSCRIBE_WINDOW_S)
    else:
        specs = request.get("windows") or []
        if not isinstance(specs, list) or not 0 < len(specs) <= MAX_WINDOWS_PER_RUN:
            raise ValueError("window count out of range")
        first_index, start_s = int(specs[0]["index"]), float(specs[0]["start_s"])
        segment_s = float(specs[0]["duration_s"])
        total_s = float(specs[-1]["start_s"]) + float(specs[-1]["duration_s"]) - start_s
        if first_index < 0 or start_s < 0 or segment_s <= 0 or total_s <= 0:
            raise ValueError("window spec out of range")
    # ffmpeg numbers the segments from 0; they are renamed to the file's window indexes below
    pattern = os.path.join(out_dir, "seg_%d.opus")
    code, timed_out = _run_ffmpeg(build_segment_argv(url, ca_file, container, start_s, total_s, segment_s, pattern),
                                  deadline)
    produced = sorted(int(m.group(1)) for name in os.listdir(out_dir)
                      if (m := re.fullmatch(r"seg_(\d+)\.opus", name)) and _regular(os.path.join(out_dir, name)))
    if timed_out and produced:
        _discard(os.path.join(out_dir, f"seg_{produced[-1]}.opus"))   # the one being written
        produced = produced[:-1]
    if not timed_out and code != 0:
        return {"status": "error", "detail": f"ffmpeg exit {code}"}
    windows: List[Dict[str, Any]] = []
    covered_end = start_s
    for k in produced:
        src = os.path.join(out_dir, f"seg_{k}.opus")
        length = _file_duration(src)
        if length <= 0:
            return {"status": "error", "detail": "segment has no duration"}
        index = first_index + k
        os.replace(src, os.path.join(out_dir, window_name(index)))
        windows.append({"index": index, "start_s": start_s + k * segment_s, "duration_s": length})
        covered_end = start_s + k * segment_s + length
    if timed_out:
        return {"status": "timeout", "windows": windows, "covered_s": covered_end - start_s, "detail": "wall in pass"}
    if not windows:
        return {"status": "error", "detail": "no audio produced"}
    covered = covered_end - start_s
    return {"status": "ok", "windows": windows, "covered_s": covered,
            "reached_bound": (not request.get("duration_known", True)) and covered >= total_s - _BOUND_SLACK_S}
