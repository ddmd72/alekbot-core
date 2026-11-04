"""A stand-in for `render_html_child.py` in the HtmlRenderEngine tests: started the way the engine starts the real child,

    python -B -I render_html_child_stub.py <body-file> <workdir> <mode> <browser-cache-dir> <sandbox>

under the real `ChildLauncher` and `limits_exec`. The body file names the behaviour (`ok` when empty). Stdlib only, so a
run costs two interpreter starts and nothing else. Never imported by production code."""
import json
import os
import signal
import sys
import time

OUTPUT_FILE = "out.bin"


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def main(argv):
    body_path, workdir = argv[0], argv[1]
    with open(body_path, "rb") as fh:
        behaviour = fh.read(64).decode().strip() or "ok"
    out = os.path.join(workdir, OUTPUT_FILE)
    if behaviour == "ok":
        _write(out, b"%PDF-stub")
    elif behaviour == "echo":   # what the child was started with: argv and the names of its environment
        _write(out, json.dumps({"argv": argv, "env": sorted(os.environ)}).encode())
    elif behaviour == "big":
        _write(out, b"x" * 4096)
    elif behaviour == "empty":
        _write(out, b"")
    elif behaviour == "missing":
        pass
    elif behaviour == "symlink":
        os.symlink(body_path, out)
    elif behaviour == "hardlink":
        _write(out + ".other", b"%PDF-linked")
        os.link(out + ".other", out)
    elif behaviour == "fifo":
        os.mkfifo(out)
    elif behaviour.startswith("exit"):
        _write(out, b"%PDF-partial")   # bytes left behind by a failed run are never collected
        os._exit(int(behaviour[4:]))
    elif behaviour.startswith("signal:"):
        number = getattr(signal, behaviour.split(":", 1)[1])
        if number != signal.SIGKILL:   # CPython ignores SIGXFSZ at startup; node and Chromium do not
            signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
        time.sleep(5)
    elif behaviour == "sleep":
        time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
