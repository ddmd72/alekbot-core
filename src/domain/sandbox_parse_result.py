"""
The parser child's result file, as the `parse.*` ops carry it (UNTRUSTED_CONTENT_SANDBOX_RFC §4.7).

Pure data and a codec, stdlib only. The child (`drive_parser/child.py`) is untrusted, so the engine validates its
result with `decode_parse_result` before it travels back as the response body, and the caller's client decodes the body
with the same function (a client may not `json.loads`, test_sandbox_architecture). Shallow checks only: closed key set,
the op's status set, scalar types, a raw-size bound. Deep checks (capture signals, text length, page arithmetic) stay with
the caller.

Public interface:
    ParseResultError                  a result that is oversized, not JSON, or outside the closed schema
    PARSE_RESULT_MAX_BYTES            bound on the raw result
    decode_parse_result(raw, op)      validated result dict (detail cut to DETAIL_MAX_CHARS)
    encode_parse_result(obj)          the response body of a decoded result: UTF-8 JSON

There is no `brief` key: the child never writes one (it is the reader's), and no identity.

`parse.container_facts` (EXTERNAL_FILES U4b, D13/O2) has its own closed key set, `CONTAINER_RESULT_KEYS`, and status
set: `zip` (the facts are given), `ole` (a legacy OLE2 file: `vba_storage`), `not_zip`, `too_large` (the member cap or the content-types cap), `error` (a ZIP the
reader could not read). The main side builds `container_facts.ContainerFacts` from the decoded dict.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, FrozenSet, Mapping, Union

from .sandbox_contract import SandboxOp

# Mirrored from `subprocess_content_extractor._RESULT_MAX_BYTES`: JSON of MAX_TEXT_CHARS (2_000_000) characters, escapes
# included, plus 1 MiB. A test pins the value to `MAX_TEXT_CHARS * 6 + 1 MiB`.
PARSE_RESULT_MAX_BYTES = 2_000_000 * 6 + 1024 * 1024
DETAIL_MAX_CHARS = 300   # the in-process adapter cuts `detail` at 300

RESULT_KEYS: FrozenSet[str] = frozenset({
    "status", "text", "pages_total", "truncated", "detail", "script_rendered", "capture_signals", "pages",
})
_EXTRACT_STATUSES = frozenset({"text", "no_text", "unsupported", "too_large", "encrypted", "error"})
# `text`, or what PdfSplitFailed can carry (its docstring: encrypted, unsupported, timeout, memory, too_large, error).
_PAGE_STATUSES = frozenset({"text", "encrypted", "unsupported", "timeout", "memory", "too_large", "error"})
CONTAINER_RESULT_KEYS: FrozenSet[str] = frozenset({
    "status", "detail", "members", "vba_project", "macro_content_type", "odf_macros",
    "vba_storage",
})
_CONTAINER_STATUSES = frozenset({"zip", "ole", "not_zip", "too_large", "error"})
# What a decoded answer of each status must carry: a `zip` its four facts, an `ole` its member count and `vba_storage`.
_CONTAINER_FACTS_NEEDED: Mapping[str, FrozenSet[str]] = {
    "zip": frozenset({"members", "vba_project", "macro_content_type", "odf_macros"}),
    "ole": frozenset({"members", "vba_storage"}),
}
_STATUSES: Mapping[SandboxOp, FrozenSet[str]] = {
    SandboxOp.PARSE_EXTRACT: _EXTRACT_STATUSES,
    SandboxOp.PARSE_PDF_PAGE_COUNT: _PAGE_STATUSES,
    SandboxOp.PARSE_PDF_WINDOWS: _PAGE_STATUSES,
    SandboxOp.PARSE_CONTAINER_FACTS: _CONTAINER_STATUSES,
}
_KEYS: Mapping[SandboxOp, FrozenSet[str]] = {
    SandboxOp.PARSE_EXTRACT: RESULT_KEYS,
    SandboxOp.PARSE_PDF_PAGE_COUNT: RESULT_KEYS,
    SandboxOp.PARSE_PDF_WINDOWS: RESULT_KEYS,
    SandboxOp.PARSE_CONTAINER_FACTS: CONTAINER_RESULT_KEYS,
}


class ParseResultError(ValueError):
    """A parser result outside the contract."""


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


_FIELD_CHECKS: Mapping[str, Callable[[Any], bool]] = {
    "text": _is_str, "detail": _is_str, "pages_total": _is_count, "pages": _is_count,
    "truncated": _is_bool, "script_rendered": _is_bool, "capture_signals": _is_dict,
    "members": _is_count, "vba_project": _is_bool, "macro_content_type": _is_bool, "odf_macros": _is_bool,
    "vba_storage": _is_bool,
}


def _validated(obj: Any, statuses: FrozenSet[str], keys: FrozenSet[str]) -> Dict[str, Any]:
    if not isinstance(obj, dict) or not set(obj) <= keys:
        raise ParseResultError("result keys outside the closed schema")
    if obj.get("status") not in statuses:
        raise ParseResultError("status outside the op's set")
    for key, ok in _FIELD_CHECKS.items():
        if key in obj and not ok(obj[key]):
            raise ParseResultError(f"bad type for {key}")
    result = dict(obj)
    if "detail" in result:
        result["detail"] = result["detail"][:DETAIL_MAX_CHARS]
    return result


def decode_parse_result(raw: Union[bytes, bytearray], op: SandboxOp) -> Dict[str, Any]:
    statuses = _STATUSES.get(op)
    if statuses is None:
        raise ParseResultError("not a parse op")
    if len(raw) > PARSE_RESULT_MAX_BYTES:
        raise ParseResultError("result too large")
    try:
        obj = json.loads(bytes(raw))
    except (ValueError, RecursionError) as exc:   # ValueError includes UnicodeDecodeError
        raise ParseResultError("result is not JSON") from exc
    result = _validated(obj, statuses, _KEYS[op])
    if op is SandboxOp.PARSE_PDF_PAGE_COUNT and result["status"] == "text" and "pages_total" not in result:
        raise ParseResultError("page count result without pages_total")
    if op is SandboxOp.PARSE_CONTAINER_FACTS:
        needed = _CONTAINER_FACTS_NEEDED.get(result["status"])
        if needed is not None and not needed <= set(result):
            raise ParseResultError("container facts without every fact")
    return result


def encode_parse_result(obj: Mapping[str, Any]) -> bytes:
    """The response body for an already decoded result (closed keys re-checked, status set was the decoder's)."""
    if not set(obj) <= RESULT_KEYS | CONTAINER_RESULT_KEYS:
        raise ParseResultError("result keys outside the closed schema")
    result = dict(obj)
    try:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:   # e.g. a lone surrogate escaped in the child's JSON
        raise ParseResultError("result cannot be encoded") from exc
