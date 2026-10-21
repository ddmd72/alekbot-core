"""
Child-side ffprobe / ffmpeg for the drive transcript extractor (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4, Δ99, Δ134).

Runs inside the unprivileged parser child after `serve` applied the limits (ffmpeg inherits them). The URL arrives in the
stdin request and lives in the ffmpeg argv only: readable by root and the parser uid, the named Δ99 secret. Nothing here
logs it, and ffmpeg's stderr is left to the parent's digest.

Public interface:
    build_probe_argv(url, ca_file) -> List[str]
    build_window_argv(url, ca_file, container, start_s, length_s, out_path) -> List[str]
    build_pass_argv(url, ca_file, container, start_s, span_s, out_path) -> List[str]
    build_cut_argv(src, rel_start_s, length_s, out_path) -> List[str]
    run_probe(request) -> dict
    run_windows(request) -> dict
"""
from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import resource
import signal
import stat
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from src.domain.drive_index_params import MEDIA_PASS_MAX_WINDOWS, TRANSCRIBE_MAX_SECONDS

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
PROTOCOL_WHITELIST = "https,tls,tcp,crypto"
# G14: a short name admits the demuxer whose name lists it ("mov" -> "mov,mp4,m4a,3gp,3g2,mj2"); copied to nested inputs.
FORMAT_WHITELIST = "mov,mp4,m4a,matroska,webm,mp3,wav,flac,ogg,aac"
# A dropped connection must neither end the read quietly (ffmpeg exits 0 after "Stream ends prematurely", which a pass
# would read as the end of the file, Δ100) nor lose it: reconnect with a Range read, and `-xerror` makes a connection
# that keeps dropping a non-zero exit.
RECONNECT_FLAGS = ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5"]
REMOTE_RUN_FLAGS = ["-xerror"]
# ffmpeg exits with `AVERROR(errno) & 0xff`, and errno numbers differ between Linux and macOS: computed at runtime.
# These are the I/O failures of a connection (a transient: the next attempt gets a fresh URL). Any other non-zero exit
# of a remote run is re-run once WITHOUT `-xerror` (reconnect kept), because `-xerror` also makes one corrupt packet
# fatal where ffmpeg would conceal it (T-R16). Named residual: a drop that reconnect cannot recover, in that re-run,
# reads as an end of the file, as a clean run's short output does.
NETWORK_ERRNOS = (errno.EIO, errno.ECONNRESET, errno.ETIMEDOUT, errno.EPIPE, errno.ECONNREFUSED)
NETWORK_TIMEOUT_US = 30_000_000       # -rw_timeout: a stalled socket ends as an error, not a wall-clock kill
MAX_WINDOWS_PER_RUN = 64
_LIMIT_SIGNALS = (-signal.SIGXCPU, -signal.SIGXFSZ)   # an rlimit ended ffmpeg: its output so far is a valid prefix
_FORMAT_NAME = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")
_BOUND_SLACK_S = 1.0                  # a pass within this of its `-t` bound reached it
_MIN_BUDGET_S = 1.0
_TRUNCATED_WINDOW_S = 2.0              # a produced window this much shorter than planned was cut short


def window_name(index: int) -> str:
    return f"window_{index}.opus"


def _input_guard(url: str, ca_file: str, container: str) -> List[str]:
    """Flags common to every run: allowlists, TLS verification (ffmpeg verifies nothing by default), the network
    timeout, and the probed demuxer pinned with `-f` when known (G14)."""
    argv = ["-protocol_whitelist", PROTOCOL_WHITELIST, "-format_whitelist", FORMAT_WHITELIST,
            "-tls_verify", "1", "-ca_file", ca_file, "-rw_timeout", str(NETWORK_TIMEOUT_US)] + RECONNECT_FLAGS
    if not _FORMAT_NAME.match(container):
        raise ValueError("container name is missing or has an invalid shape")
    return argv + ["-f", container]


def build_probe_argv(url: str, ca_file: str) -> List[str]:
    """ffprobe has no `-nostdin`; the runner gives it a closed stdin instead."""
    return [FFPROBE, "-hide_banner", "-v", "error", "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-format_whitelist", FORMAT_WHITELIST, "-tls_verify", "1", "-ca_file", ca_file,
            "-rw_timeout", str(NETWORK_TIMEOUT_US), *RECONNECT_FLAGS, "-print_format", "json",
            "-show_entries", "format=duration,format_name:stream=codec_type", "-i", url]


_OUTPUT_FLAGS = ["-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                 "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
                 "-vbr", "constrained"]


def build_window_argv(url: str, ca_file: str, container: str, start_s: float, length_s: float,
                      out_path: str, xerror: bool = True) -> List[str]:
    """One window: input-side `-ss` / `-t` (Range reads, Δ134), audio only, to `out_path`."""
    return ([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y"] + (REMOTE_RUN_FLAGS if xerror else [])
            + _input_guard(url, ca_file, container)
            + ["-ss", f"{start_s:.3f}", "-t", f"{length_s:.3f}", "-i", url] + _OUTPUT_FLAGS + ["-f", "opus", out_path])


def build_pass_argv(url: str, ca_file: str, container: str, start_s: float, span_s: float, out_path: str,
                    xerror: bool = True) -> List[str]:
    """The one remote read of a pass over a file without a duration (G35, T-R15): `-ss` only when resuming past the
    start, `-t` bounding it, one Ogg Opus file out. The windows are cut from that file locally."""
    seek = ["-ss", f"{start_s:.3f}"] if start_s > 0 else []
    return ([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y"] + (REMOTE_RUN_FLAGS if xerror else [])
            + _input_guard(url, ca_file, container)
            + seek + ["-t", f"{span_s:.3f}", "-i", url] + _OUTPUT_FLAGS + ["-f", "opus", out_path])


def build_cut_argv(src: str, rel_start_s: float, length_s: float, out_path: str) -> List[str]:
    """One window cut from the local pass file by stream copy: file protocol and Ogg only."""
    return [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
            "-protocol_whitelist", "file", "-format_whitelist", "ogg",
            "-ss", f"{rel_start_s:.3f}", "-t", f"{length_s:.3f}", "-i", src,
            "-map", "0:a:0", "-c:a", "copy", "-f", "ogg", out_path]


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


def network_exit_codes() -> frozenset:
    return frozenset((-e) & 0xFF for e in NETWORK_ERRNOS)


def _run_remote(build: Any, request: Dict[str, Any], deadline: float, out_path: str) -> Tuple[Optional[int], bool]:
    """A remote ffmpeg run under `-xerror`; a non-zero exit that is neither a limit, the wall nor a network error is
    re-run once without it (T-R16)."""
    code, timed_out = _run_ffmpeg(build(True), deadline)
    if (timed_out or code == 0 or code in _LIMIT_SIGNALS or code in network_exit_codes()
            or _hit_a_limit(request, out_path)):
        return code, timed_out
    return _run_ffmpeg(build(False), deadline)


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
        if not _FORMAT_NAME.match(container):        # extraction pins the probed demuxer (G14): never empty
            return {"status": "error", "detail": "container missing"}
        if not request.get("duration_known", True):
            return _run_pass(request, url, ca_file, container, out_dir, deadline)
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
        code, timed_out = _run_remote(
            lambda xerror: build_window_argv(url, ca_file, container, start, length, part, xerror),
            request, deadline, part)
        if timed_out:
            _discard(part)
            return {"status": "timeout", "windows": written, "detail": "wall in window"}
        if code != 0:
            _discard(part)
            return {"status": "error", "detail": f"ffmpeg exit {code}"}
        real = _file_duration(part) if _regular(part) and os.path.getsize(part) > 0 else 0.0
        if real <= 0:                    # a header-only Ogg is what a seek past the end of the audio leaves
            _discard(part)
            if start > 0 and written:    # the audio ended before this window began (T-R16)
                return {"status": "ok", "windows": written, "eof": True, "duration_s": start}
            return {"status": "error", "detail": "no audio produced"}
        os.replace(part, final)
        written.append({"index": index, "start_s": start, "duration_s": real})   # the measured length, never the plan's
        if real < length - _TRUNCATED_WINDOW_S:
            # A clean run that comes out short is the real end of the audio (the header duration overstated it).
            return {"status": "ok", "windows": written, "eof": True, "duration_s": start + real}
    return {"status": "ok", "windows": written, "eof": False}


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _hit_a_limit(request: Dict[str, Any], pass_path: str) -> bool:
    """ffmpeg handles SIGXCPU itself (finishes the file, exits 255) and Python leaves SIGXFSZ ignored (the write fails
    with EFBIG), so neither shows up as a signal exit. Told apart by the measure: CPU time used by this child's
    ffmpeg near RLIMIT_CPU, or the output at RLIMIT_FSIZE. Either way the file written so far is a valid prefix."""
    cpu_s, file_bytes = float(request.get("cpu_s", 0) or 0), int(request.get("file_bytes", 0) or 0)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    if cpu_s > 0 and usage.ru_utime + usage.ru_stime >= 0.9 * cpu_s:
        return True
    try:
        return file_bytes > 0 and os.path.getsize(pass_path) >= 0.98 * file_bytes
    except OSError:
        return False


def _no_progress(limit: bool) -> Dict[str, Any]:
    """A wall cut with nothing finished is retried next tick; a limit cut with nothing finished would end the same way
    every time (the first window does not fit the limits), so it is the file's error, not a resumable timeout."""
    if limit:
        return {"status": "error", "detail": "limit before the first window"}
    return {"status": "timeout", "windows": []}


def _run_pass(request: Dict[str, Any], url: str, ca_file: str, container: str, out_dir: str,
              deadline: float) -> Dict[str, Any]:
    """No duration (G35, T-R15): `windows` is a short contiguous slice of the standard plan. Pass 1 reads the span once
    into `pass.opus`; pass 2 cuts each planned window from it (overlap preserved) by stream copy. The duration D is read
    from the produced file; a pass shorter than its span hit the end of the file (`eof`). A pass cut by the wall or by
    SIGXCPU / SIGXFSZ leaves a valid Ogg prefix: only the windows fully inside it are cut (`timeout`)."""
    specs = request.get("windows") or []
    if not isinstance(specs, list) or not 0 < len(specs) <= MEDIA_PASS_MAX_WINDOWS:
        raise ValueError("window count out of range")
    plan = [(int(w["index"]), float(w["start_s"]), float(w["duration_s"])) for w in specs]
    if any(i < 0 or st < 0 or ln <= 0 for i, st, ln in plan) or [i for i, _, _ in plan] != list(
            range(plan[0][0], plan[0][0] + len(plan))):
        raise ValueError("window spec out of range")
    first_start, last_end = plan[0][1], plan[-1][1] + plan[-1][2]
    span = last_end - first_start
    pass_path = os.path.join(out_dir, "pass.opus")
    try:
        code, timed_out = _run_remote(
            lambda xerror: build_pass_argv(url, ca_file, container, first_start, span, pass_path, xerror),
            request, deadline, pass_path)
        limit = code in _LIMIT_SIGNALS or (code != 0 and not timed_out and _hit_a_limit(request, pass_path))
        cut = timed_out or limit
        if not cut and code != 0:
            return {"status": "error", "detail": f"ffmpeg exit {code}"}
        duration = _file_duration(pass_path) if _regular(pass_path) else 0.0
        if duration <= 0:
            return _no_progress(limit) if cut else {"status": "error", "detail": "no audio produced"}
        eof = (not cut) and duration < span - _BOUND_SLACK_S
        windows: List[Dict[str, Any]] = []
        for index, start, length in plan:
            rel = start - first_start
            if rel >= duration - _BOUND_SLACK_S:
                break                                   # the file ended before this window began
            if cut and rel + length > duration + _BOUND_SLACK_S:
                break                                   # not fully inside the killed prefix
            real = min(length, duration - rel)
            final = os.path.join(out_dir, window_name(index))
            part = final + ".part"
            code, timed_out = _run_ffmpeg(build_cut_argv(pass_path, rel, real, part), deadline)
            if timed_out:
                _discard(part)
                return {"status": "timeout", "windows": windows}
            if code != 0 or not _regular(part) or os.path.getsize(part) == 0 or _file_duration(part) <= 0:
                _discard(part)                         # header-only Ogg from a seek past the end, or a failed cut
                if code != 0:
                    return {"status": "error", "detail": f"ffmpeg cut exit {code}"}
                break
            os.replace(part, final)
            windows.append({"index": index, "start_s": start, "duration_s": real})
    finally:
        _discard(pass_path)
    if cut:
        return {"status": "timeout", "windows": windows} if windows else _no_progress(limit)
    if not windows:
        return {"status": "error", "detail": "no audio produced"}
    result: Dict[str, Any] = {"status": "ok", "windows": windows, "eof": eof,
                              "reached_bound": (not eof) and last_end >= TRANSCRIBE_MAX_SECONDS}
    if eof:
        result["duration_s"] = first_start + duration
    return result
