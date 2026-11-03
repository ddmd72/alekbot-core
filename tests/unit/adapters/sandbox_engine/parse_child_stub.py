"""A stand-in for `parse_child.py` in the ParseEngine tests: `python -B -I parse_child_stub.py`, started the way the engine
starts the real child. It goes through the real `serve` of the drive child (request on stdin, limits, atomic result file,
MemoryError exit). What it does is chosen by the JSON in the request body file (`request["path"]`): a map from the
child's index (the `N` of `result-N.json`) to a behaviour, `"*"` for every index, `ok` by default. Never imported by
production code."""
import json
import os
import signal
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from src.adapters.drive_parser.child import serve  # noqa: E402


def _behaviour(request):
    with open(request["path"], "rb") as fh:
        raw = fh.read(4096)
    spec = json.loads(raw) if raw.strip().startswith(b"{") else {}
    index = os.path.basename(request["out"]).split("-")[1].split(".")[0]
    return spec.get(index, spec.get("*", "ok"))


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def _ok(request):
    op = request["op"]
    if op == "extract":
        return {"status": "text", "text": json.dumps(request), "pages_total": 1, "truncated": False,
                "detail": "", "script_rendered": False}
    if op == "pdf_page_count":
        return {"status": "text", "pages_total": 3}
    index = os.path.basename(request["out"]).split("-")[1].split(".")[0]
    _write(request["out_path"], b"%PDF-window-" + index.encode())
    return {"status": "text", "pages": 1}


def _handle(request):
    behaviour = _behaviour(request)
    out, out_path = request["out"], request.get("out_path")
    if behaviour == "ok":
        return _ok(request)
    if behaviour.startswith("status:"):
        return {"status": behaviour.split(":", 1)[1], "detail": "d" * 400}
    if behaviour == "no_text":
        return {"status": "no_text", "pages_total": 5, "truncated": False, "detail": "scanned",
                "capture_signals": {"pages_sampled": 2}}
    if behaviour == "sleep":
        time.sleep(60)
    if behaviour == "spin":
        while True:
            pass
    if behaviour == "memory_error":
        raise MemoryError
    if behaviour == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    if behaviour.startswith("exit"):
        os._exit(int(behaviour[4:]))
    if behaviour == "no_result":
        os._exit(0)
    if behaviour == "garbage":
        _write(out, b"{not json")
        os._exit(0)
    if behaviour == "extra_key":
        return {"status": "text", "text": "x", "brief": "not allowed"}
    if behaviour == "bad_status":
        return {"status": "budget_exhausted"}
    if behaviour == "result_symlink":
        os.symlink("/etc/hosts", out)
        os._exit(0)
    if behaviour == "result_fifo":
        os.mkfifo(out)
        os._exit(0)
    if behaviour == "result_hardlink":
        _write(out + ".other", b'{"status": "text", "pages_total": 1}')
        os.link(out + ".other", out)
        os._exit(0)
    if behaviour == "result_oversized":
        _write(out, b'{"status": "text", "text": "' + b"x" * (14 * 1024 * 1024) + b'"}')
        os._exit(0)
    if behaviour.startswith("window_"):
        result = {"status": "text", "pages": 1}
        _write(out + ".part", json.dumps(result).encode())
        os.replace(out + ".part", out)
        if behaviour == "window_symlink":
            os.symlink("/etc/hosts", out_path)
        elif behaviour == "window_fifo":
            os.mkfifo(out_path)
        elif behaviour == "window_empty":
            _write(out_path, b"")
        elif behaviour == "window_missing":
            pass
        elif behaviour == "window_big":
            _write(out_path, b"x" * 4096)
        elif behaviour == "window_hardlink":
            _write(out_path + ".other", b"%PDF-x")
            os.link(out_path + ".other", out_path)
        os._exit(0)
    return {"status": "error", "detail": f"stub has no behaviour {behaviour!r}"}


if __name__ == "__main__":
    sys.exit(serve(_handle))
