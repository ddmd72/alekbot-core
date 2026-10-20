"""
Applies the child's resource limits, then replaces itself with the engine (RFC §4.6, §4.8; plan G6).

    python -B -E -s -m src.adapters.sandbox_engine.limits_exec '<limits-json>' -- argv...

The limits are in force before the engine's first instruction, so a parser or Node loads under them. Stdlib only: this
module runs as the unprivileged child and must stay tiny. Any limit that cannot be applied on Linux exits
EXIT_BAD_REQUEST and the engine never runs; macOS refuses RLIMIT_AS / RLIMIT_DATA and is tolerated (development only,
as in drive_parser.child.apply_limits).

limits-json keys: memory_bytes, memory_mode ("as" | "data"), cpu_s, file_bytes, nproc, nofile; optional cwd.
"""
from __future__ import annotations

import json
import math
import os
import resource
import sys
from typing import Any, List, Mapping, Optional

EXIT_BAD_REQUEST = 4     # same meaning as drive_parser.child: unreadable request or limits not applied
EXIT_EXEC_FAILED = 5     # the engine binary could not be executed

CPU_GRACE_S = 2          # hard RLIMIT_CPU above the soft one: SIGXCPU first, SIGKILL only if it is ignored

# Chosen here (RFC §11 gives no figure); S0-f sets it. Per-uid counts: the child's uid runs nothing else.
NPROC_DEFAULT = 64
# Chosen here (RFC §11 gives no figure); S0-f sets it.
NOFILE_DEFAULT = 64
# G6: S0-f decides. "as" = RLIMIT_AS (§11); "data" = RLIMIT_DATA, for a Node child that cannot start under RLIMIT_AS.
MEMORY_MODE_DEFAULT = "as"

_MEMORY_RLIMITS = {"as": resource.RLIMIT_AS, "data": resource.RLIMIT_DATA}
_REQUIRED = ("memory_bytes", "cpu_s", "file_bytes", "nproc", "nofile")


def _fail(code: int, reason: str) -> int:
    sys.stderr.write(f"limits_exec: {reason}\n")
    return code


def _parse(raw: str) -> Optional[Mapping[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or any(key not in data for key in _REQUIRED):
        return None
    if data.get("memory_mode", MEMORY_MODE_DEFAULT) not in _MEMORY_RLIMITS:
        return None
    return data


def apply_limits(spec: Mapping[str, Any]) -> None:
    """Raises (ValueError / OSError / TypeError) when a limit cannot be applied on Linux."""
    cpu_s = int(math.ceil(float(spec["cpu_s"])))
    memory = int(spec["memory_bytes"])
    file_bytes = int(spec["file_bytes"])
    nproc = int(spec["nproc"])
    nofile = int(spec["nofile"])
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + CPU_GRACE_S))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
    try:
        resource.setrlimit(_MEMORY_RLIMITS[spec.get("memory_mode", MEMORY_MODE_DEFAULT)], (memory, memory))
    except (ValueError, OSError):
        if sys.platform.startswith("linux"):
            raise
        # A dev machine: the other limits and the wall clock still hold.


def main(argv: List[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        return _fail(EXIT_BAD_REQUEST, "usage: <limits-json> -- argv...")
    spec = _parse(argv[1])
    if spec is None:
        return _fail(EXIT_BAD_REQUEST, "unreadable limits")
    engine = argv[3:]
    try:
        apply_limits(spec)
        if spec.get("cwd"):
            os.chdir(str(spec["cwd"]))
    except (ValueError, OSError, TypeError, OverflowError) as exc:
        return _fail(EXIT_BAD_REQUEST, f"limit not applied: {type(exc).__name__}")
    try:
        os.execvpe(engine[0], engine, dict(os.environ))
    except OSError as exc:
        return _fail(EXIT_EXEC_FAILED, f"exec failed: {type(exc).__name__}")
    return EXIT_EXEC_FAILED  # unreachable: execvpe replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv))
