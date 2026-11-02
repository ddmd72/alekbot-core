"""A media child for the relay tests: `python -m tests.unit.adapters.drive_media_relay_child_stub`. It goes through the
real `serve`, writes what it received (its request, argv and environment) as the text of `window_0.opus`, and, when
the url is the relay's loopback url, reads it once and notes the status. Never imported by production code."""
import json
import os
import sys
import urllib.request

from src.adapters.drive_parser.child import serve


def _handle(request):
    url = str(request.get("url", ""))
    fetched = None
    if url.startswith("http://127.0.0.1:"):
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-99"}), timeout=10) as resp:
            fetched = {"status": resp.status, "length": len(resp.read())}
    dump = {"request": request, "argv": sys.argv, "env": dict(os.environ), "fetched": fetched}
    with open(os.path.join(request["out_dir"], "window_0.opus"), "w") as fh:
        json.dump(dump, fh)
    return {"status": "ok", "windows": [{"index": 0, "start_s": 0.0, "duration_s": 10.0}]}


if __name__ == "__main__":
    sys.exit(serve(_handle))
