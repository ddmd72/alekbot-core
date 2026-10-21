"""A misbehaving media child for the launcher tests: `python -m tests.unit.adapters.drive_media_child_stub`. It goes
through the real `serve`; the request's `url` (a label here, not a URL) picks what it does. Never imported by production
code."""
import json
import os
import subprocess
import sys
import time

from src.adapters.drive_parser.child import serve


def _window(out_dir, index=0, data=b"OggS" + b"\0" * 64):
    path = os.path.join(out_dir, f"window_{index}.opus")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _handle(request):
    label, out_dir = request.get("url", ""), request.get("out_dir", "")
    entry = {"index": 0, "start_s": 0.0, "duration_s": 10.0}
    if label == "sleep_group":                # a grandchild in the group, like ffmpeg
        grandchild = subprocess.Popen(["sleep", "60"])
        with open(os.path.join(out_dir, "grandchild.pid"), "w") as fh:
            fh.write(str(grandchild.pid))
        time.sleep(60)
    if label == "ok":
        _window(out_dir)
        return {"status": "ok", "windows": [entry]}
    if label == "symlink":
        target = os.path.join(out_dir, "elsewhere")
        with open(target, "wb") as fh:
            fh.write(b"secret")
        os.symlink(target, os.path.join(out_dir, "window_0.opus"))
        return {"status": "ok", "windows": [entry]}
    if label == "fifo":
        os.mkfifo(os.path.join(out_dir, "window_0.opus"))
        return {"status": "ok", "windows": [entry]}
    if label == "oversize":
        _window(out_dir, data=b"x" * 5000)
        return {"status": "ok", "windows": [entry]}
    if label == "missing":
        return {"status": "ok", "windows": [entry]}
    if label == "bad_entry":
        return {"status": "ok", "windows": [{"index": -1, "start_s": 0.0, "duration_s": 1.0}]}
    if label == "env":
        with open(os.path.join(out_dir, "env.json"), "w") as fh:
            json.dump(dict(os.environ), fh)
        return {"status": "ok", "windows": []}
    return {"status": "error", "detail": "stub"}


if __name__ == "__main__":
    sys.exit(serve(_handle))
