"""
Parse engine child entry (RFC §4.6, §4.7; plan P1): runs under `limits_exec`, as the engine uid.

    python -B -I <path>/parse_child.py        (the request JSON on stdin, as the drive parser child reads it)

Runs the drive parser's own child, `src.adapters.drive_parser.child`, unchanged. Started by file path, not
`-m src.adapters.drive_parser.child`: `limits_exec` has already changed into the workdir, which the engine uid can write,
so `-m` would put that directory on sys.path (it would also not find `src`). Under `-I` neither the script's directory nor
the cwd is on the path; this module appends the repository root last, so nothing in it can shadow a stdlib module, then
hands over by name. The child module is a string, never an import (REQ-ARCH-23: an engine module imports no other
adapter subpackage), and this file imports no parser library.

The exit status is the drive child's: 0 result written, 3 memory, 4 unreadable request or limits.
"""
from __future__ import annotations

import os
import runpy
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

DRIVE_CHILD_MODULE = "src.adapters.drive_parser.child"


def main() -> int:
    runpy.run_module(DRIVE_CHILD_MODULE, run_name="__main__")   # the module ends with sys.exit(main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
