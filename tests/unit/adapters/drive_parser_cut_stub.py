"""A drive parser child that answers the page-window ops with results it cannot back, for the adapter's checks:
`python -m tests.unit.adapters.drive_parser_cut_stub`. The source file's name picks the behaviour. Never imported by
production code."""
import os
import sys

from src.adapters.drive_parser.child import serve


def _handle(request):
    name = os.path.basename(request.get("path", ""))
    if name == "bad_count":
        return {"status": "text", "pages_total": "many"}
    if name == "no_window":
        return {"status": "text", "pages": 1}                   # claims a window it never wrote
    if name == "symlink_window":
        os.symlink("/etc/hosts", request["out_path"])           # a window that is a link to another file
        return {"status": "text", "pages": 1}
    return {"status": "error", "detail": f"stub has no behaviour {name!r}"}


if __name__ == "__main__":
    sys.exit(serve(_handle))
