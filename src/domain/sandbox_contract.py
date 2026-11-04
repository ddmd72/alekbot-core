"""
Sandbox call contract (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.7 call contract, §4.8 limits,
§11 parameters, invariant I3). Pure data and codecs.

One module because these types are one contract, read together by the caller's client, the server and the
engines (CLAUDE.md's one-class rule is waived here on purpose; precedent `drive_match`).

Closed schema: the request header `X-Sandbox-Meta` and the response header `X-Sandbox-Outcome` are base64url
JSON with a fixed key set. A key outside the set is an error, which is how invariant I3 (no user id, account id,
item id, file name or path ever reaches the service) is enforced on the wire. A test pins the field sets.

Public interface:
    SandboxOp, SandboxStatus            the op and outcome vocabularies
    SandboxLimits, SandboxMeta          the request metadata (`windows`: the page-window plan of `parse.pdf_windows`)
    SandboxOutcome                      the response envelope
    SandboxEngineResult, SandboxEngine  what an engine returns to the server (the web layer may not import adapters)
    new_request_id()                    random correlation id, the only correlation
    encode_meta / decode_meta           header codec for SandboxMeta
    encode_outcome / decode_outcome     header codec for SandboxOutcome
    clamp_limits(requested, ceiling)    a caller may only lower the service's limits
    SandboxContractError                any malformed or oversized envelope
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, Union, runtime_checkable

META_HEADER = "X-Sandbox-Meta"
OUTCOME_HEADER = "X-Sandbox-Outcome"

META_MAX_BYTES = 8192  # RFC §4.7 / §11: `meta` size 8 KiB

# The request is the raw file (no framing), so its cap is the PDF product cap (owner decision 2026-11-03; D6) plus a
# margin. A mirror of `drive_index_params.MAX_PDF_DOWNLOAD_BYTES`, not an import: that would grow the render image's
# closure. A test pins the two in step.
PDF_PRODUCT_CAP_BYTES = 100 * 1024 * 1024
REQUEST_MARGIN_BYTES = 1024 * 1024
REQUEST_CAP_BYTES = PDF_PRODUCT_CAP_BYTES + REQUEST_MARGIN_BYTES
# A parse result is bounded by the same figure: `parse.extract` by ~13 MB of JSON, `parse.pdf_windows` by a frame of
# windows cut from a PDF of at most the product cap. Streamed, never held whole by the server.
RESPONSE_CAP_BYTES = REQUEST_CAP_BYTES
# The RFC gives no number for the failure text of a script (§4.9 only says it travels back for repair).
# Initial value 16 KiB, chosen to hold a Node stack trace; revisit after S0-h (limits on the new host).
ERROR_TEXT_CAP_BYTES = 16 * 1024

# RFC §4.8 / §11 child limits, until S0-g re-measures them: RLIMIT_AS 1 GiB, RLIMIT_CPU 90 s, wall 120 s,
# RLIMIT_FSIZE 64 MiB. Revisit after S0-g/S0-h.
CEILING_WALL_S = 120.0
CEILING_MEMORY_BYTES = 1 << 30
CEILING_CPU_S = 90.0
CEILING_FILE_BYTES = 64 * 1024 * 1024

# Windows one `parse.pdf_windows` call may cut. A mirror of `drive_index_params.MODEL_EXTRACT_MAX_PAGES`, not an import:
# that would grow the render image's closure. A test pins the two equal.
SANDBOX_MAX_WINDOWS = 200

OUTCOME_DETAIL_MAX_CHARS = 256
OUTCOME_STATS_MAX_ITEMS = 16
_STATS_KEY_MAX_CHARS = 64

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_KIND_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")

Number = Union[int, float]


class SandboxContractError(ValueError):
    """A malformed, oversized or non-closed-schema envelope."""


class SandboxOp(str, Enum):
    """Ops are added by the slice that implements them, not pre-declared."""
    RENDER_DOCX = "render.docx"
    RENDER_PDF = "render.pdf"
    RENDER_PNG = "render.png"
    PARSE_EXTRACT = "parse.extract"
    PARSE_PDF_PAGE_COUNT = "parse.pdf_page_count"
    PARSE_PDF_WINDOWS = "parse.pdf_windows"


class SandboxStatus(str, Enum):
    OK = "ok"
    SCRIPT_ERROR = "script_error"
    TIMEOUT = "timeout"
    EMPTY_OUTPUT = "empty_output"
    TOO_LARGE = "too_large"
    MEMORY = "memory"   # a killed parser child leaves no result (the parse ops)
    RECYCLING = "recycling"
    UNCONFIGURED = "unconfigured"
    BAD_REQUEST = "bad_request"
    ERROR = "error"


@dataclass(frozen=True)
class SandboxLimits:
    wall_s: float
    memory_bytes: int
    cpu_s: float
    file_bytes: int


@dataclass(frozen=True)
class SandboxMeta:
    request_id: str
    limits: SandboxLimits
    kind: Optional[str] = None
    ext: Optional[str] = None
    windows: Optional[Tuple[Tuple[int, int], ...]] = None   # (first_page, page_count) per window, in order


@dataclass(frozen=True)
class SandboxOutcome:
    status: SandboxStatus
    detail: str = ""
    stats: Mapping[str, Number] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.detail) > OUTCOME_DETAIL_MAX_CHARS:
            raise SandboxContractError("outcome detail too long")
        if len(self.stats) > OUTCOME_STATS_MAX_ITEMS:
            raise SandboxContractError("too many outcome stats")
        for key, value in self.stats.items():
            if not key or len(key) > _STATS_KEY_MAX_CHARS:
                raise SandboxContractError("bad outcome stats key")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SandboxContractError("outcome stats values must be numbers")


@dataclass(frozen=True)
class SandboxEngineResult:
    """What an engine reports to the server after one run.

    When `output_path` is set it lives in a per-request directory owned by the server; the caller removes
    `dirname(output_path)` once it has streamed the output."""
    status: SandboxStatus
    output_path: Optional[str]
    detail: str
    peak_rss_bytes: int
    wall_s: float


@runtime_checkable
class SandboxEngine(Protocol):
    async def run(
        self, body_path: str, workdir: str, limits: SandboxLimits, meta: SandboxMeta,
    ) -> SandboxEngineResult: ...


def new_request_id() -> str:
    """128 random bits, URL-safe (22 characters)."""
    return secrets.token_urlsafe(16)


def _b64_encode(obj: Dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64_decode(header: str, max_bytes: int) -> Dict[str, Any]:
    if not isinstance(header, str) or not header or len(header) > max_bytes:
        raise SandboxContractError("envelope empty or too large")
    try:
        raw = base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))
        obj = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise SandboxContractError("envelope is not base64url JSON") from exc
    if not isinstance(obj, dict):
        raise SandboxContractError("envelope must be a JSON object")
    return obj


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_LIMIT_KEYS = frozenset({"wall_s", "memory_bytes", "cpu_s", "file_bytes"})
_META_KEYS = frozenset({"request_id", "limits", "kind", "ext", "windows"})
_OUTCOME_KEYS = frozenset({"status", "detail", "stats"})


def _checked_windows(raw: Any) -> Tuple[Tuple[int, int], ...]:
    """A window plan: 1..SANDBOX_MAX_WINDOWS pairs of ints (no bools), first_page >= 0, page_count >= 1."""
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= SANDBOX_MAX_WINDOWS:
        raise SandboxContractError("windows must be a list of 1..SANDBOX_MAX_WINDOWS pairs")
    pairs = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise SandboxContractError("a window must be [first_page, page_count]")
        first_page, page_count = pair
        if any(isinstance(v, bool) or not isinstance(v, int) for v in pair):
            raise SandboxContractError("window values must be integers")
        if first_page < 0 or page_count < 1:
            raise SandboxContractError("window out of range")
        pairs.append((first_page, page_count))
    return tuple(pairs)


def encode_meta(meta: SandboxMeta) -> str:
    obj: Dict[str, Any] = {
        "request_id": meta.request_id,
        "limits": {
            "wall_s": meta.limits.wall_s,
            "memory_bytes": meta.limits.memory_bytes,
            "cpu_s": meta.limits.cpu_s,
            "file_bytes": meta.limits.file_bytes,
        },
    }
    if meta.kind is not None:
        obj["kind"] = meta.kind
    if meta.ext is not None:
        obj["ext"] = meta.ext
    if meta.windows is not None:
        obj["windows"] = [list(pair) for pair in _checked_windows(meta.windows)]
    encoded = _b64_encode(obj)
    if len(encoded) > META_MAX_BYTES:
        raise SandboxContractError("meta exceeds 8 KiB")
    return encoded


def decode_meta(header: str) -> SandboxMeta:
    obj = _b64_decode(header, META_MAX_BYTES)
    if not set(obj) <= _META_KEYS or not {"request_id", "limits"} <= set(obj):
        raise SandboxContractError("meta keys outside the closed schema")
    request_id = obj["request_id"]
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise SandboxContractError("bad request_id")
    raw_limits = obj["limits"]
    if not isinstance(raw_limits, dict) or set(raw_limits) != _LIMIT_KEYS:
        raise SandboxContractError("limits keys outside the closed schema")
    if not all(_is_number(v) and math.isfinite(v) for v in raw_limits.values()):
        raise SandboxContractError("limits must be finite numbers")
    kind = obj.get("kind")
    ext = obj.get("ext")
    if kind is not None and (not isinstance(kind, str) or not _KIND_RE.fullmatch(kind)):
        raise SandboxContractError("bad kind")
    if ext is not None and (not isinstance(ext, str) or not _EXT_RE.fullmatch(ext)):
        raise SandboxContractError("bad ext")
    windows = _checked_windows(obj["windows"]) if "windows" in obj else None
    limits = SandboxLimits(
        wall_s=float(raw_limits["wall_s"]),
        memory_bytes=int(raw_limits["memory_bytes"]),
        cpu_s=float(raw_limits["cpu_s"]),
        file_bytes=int(raw_limits["file_bytes"]),
    )
    return SandboxMeta(request_id=request_id, limits=limits, kind=kind, ext=ext, windows=windows)


def encode_outcome(outcome: SandboxOutcome) -> str:
    return _b64_encode(
        {"status": outcome.status.value, "detail": outcome.detail, "stats": dict(outcome.stats)}
    )


def decode_outcome(header: str) -> SandboxOutcome:
    obj = _b64_decode(header, META_MAX_BYTES)
    if set(obj) != _OUTCOME_KEYS:
        raise SandboxContractError("outcome keys outside the closed schema")
    try:
        status = SandboxStatus(obj["status"])
    except ValueError as exc:
        raise SandboxContractError("unknown outcome status") from exc
    detail = obj["detail"]
    stats = obj["stats"]
    if not isinstance(detail, str) or not isinstance(stats, dict):
        raise SandboxContractError("bad outcome field types")
    return SandboxOutcome(status=status, detail=detail, stats=stats)


def clamp_limits(requested: SandboxLimits, ceiling: SandboxLimits) -> SandboxLimits:
    """The caller's limits may only lower the service's. A non-positive request is an error, not a no-limit."""
    for value in (requested.wall_s, requested.memory_bytes, requested.cpu_s, requested.file_bytes):
        if not math.isfinite(value) or not value > 0:
            raise SandboxContractError("limits must be finite and positive")
    return SandboxLimits(
        wall_s=min(requested.wall_s, ceiling.wall_s),
        memory_bytes=min(requested.memory_bytes, ceiling.memory_bytes),
        cpu_s=min(requested.cpu_s, ceiling.cpu_s),
        file_bytes=min(requested.file_bytes, ceiling.file_bytes),
    )
