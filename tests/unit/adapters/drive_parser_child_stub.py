"""A misbehaving drive extraction child for the sandbox tests: `python -m tests.unit.adapters.drive_parser_child_stub`.
It goes through the real `serve` (limits, result file, MemoryError exit), and the request's `kind` (for a split, the
file name) picks what it does. Never imported by production code."""
import json
import os
import signal
import sys
import time

from src.adapters.drive_parser.child import serve


def _handle(request):
    path = request.get("path", "")
    kind = request.get("kind") or os.path.basename(path)      # split requests carry no kind
    if kind == "sleep":
        with open(path + ".pid", "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(60)
    if kind == "spin":
        while True:
            pass
    if kind == "memory_error":
        raise MemoryError
    if kind == "import_enomem":
        raise ImportError("libonnxruntime.so.1: failed to map segment from shared object")
    if kind == "allocate":
        blocks = []
        while True:
            blocks.append(bytearray(64 * 1024 * 1024))
    if kind == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if kind == "exit7":
        os._exit(7)
    if kind == "no_result":
        os._exit(0)
    if kind == "garbage":
        with open(request["out"], "w") as fh:
            fh.write("{not json")
        os._exit(0)
    if kind == "bad_status":
        return {"status": "budget_exhausted", "text": ""}
    if kind == "bad_shape":
        return {"status": "text", "text": 42}
    if kind == "huge_text":
        return {"status": "text", "text": "x" * (int(request.get("chars", 0))), "pages_total": 0,
                "truncated": False}
    if kind == "env":
        return {"status": "text", "text": json.dumps(dict(os.environ)), "pages_total": 0, "truncated": False}
    if kind == "mark":
        with open(path, "a") as fh:
            fh.write(f"start {time.monotonic()}\n")
        time.sleep(0.3)
        with open(path, "a") as fh:
            fh.write(f"end {time.monotonic()}\n")
        return {"status": "text", "text": "ok", "pages_total": 0, "truncated": False}
    if kind == "escape":
        return {"status": "text", "paths": ["/etc/hosts"], "pages_total": 1, "truncated": False}
    if kind == "split_encrypted":
        return {"status": "encrypted", "detail": "user password"}
    return {"status": "error", "detail": f"stub has no behaviour {kind!r}"}


if __name__ == "__main__":
    sys.exit(serve(_handle))
