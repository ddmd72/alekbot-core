"""A stand-in for `media_child.py` in the MediaEngine tests: `python -B -I media_child_stub.py <data_fd> <record_fd>`,
started the way the engine starts the real child (through the launcher and `limits_exec`, request JSON on stdin).
What it does is chosen by `behaviour.json` in the request's `out_dir` (the workdir the test made), `ok` by default.
It writes raw 13-byte records itself, so it can break every bound the engine's ledger holds. Never imported by
production code."""
import json
import os
import struct
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from src.domain.sandbox_tunnel import FrameReader, FrameType  # noqa: E402

RECORD = struct.Struct(">BIQ")


def _record(fd, kind, stream_id, offset=0):
    os.write(fd, RECORD.pack(kind, stream_id, offset))


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def _result(request, result):
    _write(request["out"] + ".part", json.dumps(result).encode())
    os.replace(request["out"] + ".part", request["out"])


def _windows_result(request, **extra):
    windows = [{"index": w["index"], "start_s": w["start_s"], "duration_s": w["duration_s"]}
               for w in request["windows"]]
    return {"status": "ok", "windows": windows, "eof": False, **extra}


def _window_path(request, index):
    return os.path.join(request["out_dir"], f"window_{index}.opus")


def _read_until_end(data_fd, stream_id):
    """The DATA bytes of `stream_id` until its END: (bytes, END value)."""
    reader = FrameReader(frozenset({FrameType.DATA, FrameType.END}))
    got = bytearray()
    while True:
        chunk = os.read(data_fd, 1 << 16)
        if not chunk:
            return bytes(got), None
        for frame in reader.feed(chunk):
            if frame.id != stream_id:
                continue
            if frame.kind is FrameType.END:
                return bytes(got), frame.value
            got += frame.payload


def main():
    data_fd, record_fd = int(sys.argv[1]), int(sys.argv[2])
    request = json.loads(sys.stdin.buffer.read())
    try:
        with open(os.path.join(request["out_dir"], "behaviour.json")) as fh:
            behaviour = json.load(fh)
    except FileNotFoundError:
        behaviour = "ok"
    _write(os.path.join(request["out_dir"], "seen_request.json"), json.dumps(request).encode())
    size = request["source_bytes"]
    if behaviour == "ok":
        if request["op"] == "media_probe":
            _result(request, {"status": "ok", "duration_s": 12.5, "duration_known": True, "has_audio": True,
                              "container": "mov,mp4,m4a,3gp,3g2,mj2"})
        else:
            for w in request["windows"]:
                _write(_window_path(request, w["index"]), b"OggS-window-%d" % w["index"])
            _result(request, _windows_result(request))
        return 0
    if behaviour == "echo":                       # OPEN 1 at 2, take the bytes to END, CLOSE, report them
        _record(record_fd, FrameType.OPEN, 1, 2)
        got, end = _read_until_end(data_fd, 1)
        _record(record_fd, FrameType.CLOSE, 1)
        _write(os.path.join(request["out_dir"], "echo.bin"), got)
        _result(request, {"status": "ok", "duration_s": 0, "duration_known": False, "has_audio": end == 0,
                          "container": "mp3"})
        return 0
    if behaviour == "open_beyond":
        _record(record_fd, FrameType.OPEN, 1, size)
    elif behaviour == "many_opens":
        for stream_id in range(1, 258):
            _record(record_fd, FrameType.OPEN, stream_id, 0)
            _record(record_fd, FrameType.CLOSE, stream_id)
    elif behaviour == "three_open":
        for stream_id in (1, 2, 3):
            _record(record_fd, FrameType.OPEN, stream_id, 0)
    elif behaviour == "bad_record":
        _record(record_fd, FrameType.DATA, 1, 0)
    elif behaviour == "open_and_wait":           # the caller then breaks a bound from its side
        _record(record_fd, FrameType.OPEN, 1, 0)
    elif behaviour == "sleep":
        pass
    elif behaviour == "exit3":
        os._exit(3)
    elif behaviour == "no_result":
        return 0
    elif behaviour == "child_error":
        _result(request, {"status": "error", "detail": "ffmpeg exit 183"})
        return 0
    elif behaviour.startswith("window_"):
        first = request["windows"][0]["index"]
        path = _window_path(request, first)
        if behaviour == "window_symlink":
            os.symlink("/etc/hosts", path)
        elif behaviour == "window_fifo":
            os.mkfifo(path)
        elif behaviour == "window_hardlink":
            _write(path + ".other", b"OggS")
            os.link(path + ".other", path)
        elif behaviour == "window_big":
            _write(path, b"x" * 4096)
        elif behaviour == "window_missing":
            pass
        for w in request["windows"][1:]:
            _write(_window_path(request, w["index"]), b"y" * 400)
        _result(request, _windows_result(request))
        return 0
    elif behaviour == "windows_400":            # every window 400 bytes: the response cap test
        for w in request["windows"]:
            _write(_window_path(request, w["index"]), b"z" * 400)
        _result(request, _windows_result(request, eof=True, duration_s=30.0))
        return 0
    time.sleep(60)                                 # killed by the engine (or the wall clock) before this ends
    return 0


if __name__ == "__main__":
    sys.exit(main())
