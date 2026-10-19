"""
The drive parser's child process (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.3):
`python -B -E -s -m src.adapters.drive_parser.child`, one process per file, started by
`SubprocessContentExtractor`.

Protocol: one JSON request on stdin. The result is JSON written to the request's `out` path (a temp name, then a
rename), never to stdout: a parser that prints cannot corrupt it, and a killed child leaves no partial result.
Exit 0 = result written; EXIT_MEMORY = MemoryError; EXIT_BAD_REQUEST = unreadable request or limits not applied;
a negative return code = killed by a signal (RLIMIT_CPU sends SIGXCPU, the parent's wall clock SIGKILL).
The limits are applied before any parser library is imported.

Public interface:
    EXIT_MEMORY, EXIT_BAD_REQUEST, CPU_GRACE_S
    apply_limits(memory_bytes, cpu_s, file_bytes) -> None
    is_memory_failure(exc) -> bool
    serve(handler) -> int
    dispatch(request) -> dict
    main() -> int
"""
from __future__ import annotations

import errno
import json
import logging
import os
import resource
import sys
import traceback
import warnings
from typing import Any, Callable, Dict

EXIT_MEMORY = 3
EXIT_BAD_REQUEST = 4
CPU_GRACE_S = 2                  # hard RLIMIT_CPU above the soft one: SIGXCPU first, SIGKILL only if it is ignored
_MAX_REQUEST_BYTES = 64 * 1024


def apply_limits(memory_bytes: int, cpu_s: int, file_bytes: int) -> None:
    """RLIMIT_CORE 0 (a SIGXCPU core dump of a 1 GiB child would land on Cloud Run's in-memory disk), RLIMIT_CPU,
    RLIMIT_FSIZE (one file the child writes) and RLIMIT_AS. macOS refuses RLIMIT_AS; only Linux must honour it."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + CPU_GRACE_S))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError):
        if sys.platform.startswith("linux"):
            raise
        # A dev machine: the CPU, file and wall-clock limits still hold (S1 checks RLIMIT_AS on gen2).


def is_memory_failure(exc: BaseException) -> bool:
    """MemoryError, or what RLIMIT_AS turns a lazy import or an mmap into: dlopen's "failed to map segment" and
    ENOMEM. Such a file is `memory` (per-file transient), never `error` (which routes a PDF to the reader)."""
    if isinstance(exc, MemoryError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return True
    message = str(exc)
    return isinstance(exc, (ImportError, OSError)) and (
        "failed to map segment" in message or "Cannot allocate memory" in message)


def serve(handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> int:
    """Read the request, apply its limits, run `handler`, write its result. Shared with the tests' stub child."""
    try:
        request = json.loads(sys.stdin.buffer.read(_MAX_REQUEST_BYTES))
        out = str(request["out"])
        apply_limits(int(request["memory_bytes"]), int(request["cpu_s"]), int(request["file_bytes"]))
    except Exception:  # the parent maps this exit code to `error`; the traceback goes to its stderr file
        traceback.print_exc()
        return EXIT_BAD_REQUEST
    logging.disable(logging.CRITICAL)        # pdfminer & co. warn per object: a flood would fill the stderr file
    warnings.simplefilter("ignore")
    try:
        result = handler(request)
        partial = out + ".part"
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        os.replace(partial, out)
    except BaseException as exc:
        if is_memory_failure(exc):
            os._exit(EXIT_MEMORY)            # nothing else is safe to allocate now
        raise
    return 0


def dispatch(request: Dict[str, Any]) -> Dict[str, Any]:
    """The readers load here, after `serve` applied the limits."""
    op = request.get("op")
    if op == "extract":
        from src.adapters.drive_parser.readers import read_content

        outcome = read_content(str(request["path"]), str(request["kind"]))
        result: Dict[str, Any] = {"status": outcome.status.value, "text": outcome.text,
                                  "pages_total": outcome.pages_total, "truncated": outcome.truncated,
                                  "detail": outcome.detail}
        if outcome.script_rendered:          # parser@2 (§25.3); absent means False
            result["script_rendered"] = True
        if outcome.capture_signals is not None:   # parser@3 (§26.3): PDFs only; absent means none
            from src.domain.drive_capture import signals_to_json

            result["capture_signals"] = signals_to_json(outcome.capture_signals)
        return result
    if op in ("pdf_page_count", "cut_pdf_window"):
        from src.adapters.drive_parser.readers import cut_pdf_window, pdf_page_count
        from src.domain.drive_extraction import PdfSplitFailed

        try:
            if op == "pdf_page_count":
                return {"status": "text", "pages_total": pdf_page_count(str(request["path"]))}
            pages = cut_pdf_window(str(request["path"]), int(request["first_page"]), int(request["page_count"]),
                                   str(request["out_path"]))
            return {"status": "text", "pages": pages}
        except PdfSplitFailed as exc:
            return {"status": exc.status.value, "detail": exc.detail}
    return {"status": "error", "detail": f"unknown op {op!r}"}


def main() -> int:
    return serve(dispatch)


if __name__ == "__main__":
    sys.exit(main())
