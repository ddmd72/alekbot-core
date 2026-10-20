"""
Sandbox server app (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.6 one request per instance, §4.7 call
contract, §4.9 failure semantics, §4.14 observability; plan gaps G2 and G8).

Routes: `POST /v1/<op>` and `GET /health`. The handler never reads a payload: it copies the request body to
`<work_root>/<request_id>/body` and hands the path to the op's engine. The module imports no document, image or
archive library. Engines arrive as a `Mapping[SandboxOp, SandboxEngine]` of domain types (the web layer may not
import adapters); the caller gate and the recycler are injected (`SandboxGate`, `SandboxRecycler`).

`POST /v1/<op>` order: (1) caller gate: 401 unauthenticated, 403 valid token but op not allowed; (2) unknown op
404; (3) draining or busy 503 `recycling`; (4) `Content-Length` pre-check against `REQUEST_CAP_BYTES` 413;
(5) decode `X-Sandbox-Meta` (closed schema) 400; (6) stream the body to the workdir, counting bytes ourselves
(Quart does not enforce `MAX_CONTENT_LENGTH` on a body without `Content-Length`) 413; (7) clamp limits; (8) run
the engine; (9) answer with `X-Sandbox-Outcome` always present; body = the output bytes (ok) or the failure text,
`text/plain`, capped at `ERROR_TEXT_CAP_BYTES` (G2); (10) after the answer, tell the recycler once.

Recycling (G8): under policy `always` every request that passed the gate and began writing payload bytes ends the
instance, whatever its outcome; a request refused before any payload byte does not. Such an answer carries
`Connection: close` (draining is visible, §4.6); the HTTP/2 GOAWAY comes from the graceful shutdown (T5).

The output is streamed only from `SandboxEngineResult.output_path` (a server-owned file outside the workdir):
the file is opened first, then the workdir and `dirname(output_path)` are removed, so a request that is cancelled
or never iterated leaves nothing behind. One bounded log line per request, with the closed field set of §4.14:
nothing from the meta besides `request_id`, no names, no URL, no engine output.

Three tiny Protocols live here by design (R2: gate decision, gate, recycler), next to the one blueprint factory.
"""
from __future__ import annotations

import os
import pwd
import shutil
import stat
import time
from types import TracebackType
from typing import Any, AsyncIterator, Mapping, Optional, Protocol

from quart import Blueprint, Response, request
from quart.wrappers.response import ResponseBody
from werkzeug.exceptions import RequestEntityTooLarge

from ..domain.sandbox_contract import (
    CEILING_WALL_S,
    ERROR_TEXT_CAP_BYTES,
    META_HEADER,
    OUTCOME_HEADER,
    REQUEST_CAP_BYTES,
    RESPONSE_CAP_BYTES,
    SandboxContractError,
    SandboxEngine,
    SandboxEngineResult,
    SandboxLimits,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
    clamp_limits,
    decode_meta,
    encode_outcome,
)
from ..utils.logger import logger

RECYCLE_ALWAYS = "always"
RECYCLE_NEVER = "never"
TIMEOUT_MARGIN_S = 30.0  # Quart BODY_TIMEOUT / RESPONSE_TIMEOUT sit above the op's wall ceiling by this much
_CHUNK = 64 * 1024
_TRUNCATION_MARKER = "\n[truncated]"
_UNKNOWN_OP = "unknown"

# Closed HTTP mapping of the engine statuses; the header decides, the HTTP code is for humans and proxies.
_HTTP_STATUS = {
    SandboxStatus.OK: 200,
    SandboxStatus.SCRIPT_ERROR: 422,
    SandboxStatus.EMPTY_OUTPUT: 422,
    SandboxStatus.TIMEOUT: 504,
    SandboxStatus.TOO_LARGE: 413,
    SandboxStatus.RECYCLING: 503,
    SandboxStatus.UNCONFIGURED: 503,
    SandboxStatus.BAD_REQUEST: 400,
    SandboxStatus.ERROR: 500,
}


class GateDecision(Protocol):
    @property
    def allowed(self) -> bool: ...

    @property
    def reason(self) -> Optional[str]: ...  # "unauthenticated" | "forbidden_op"


class SandboxGate(Protocol):
    def authorize(self, authorization: Optional[str], op: str) -> GateDecision: ...


class SandboxRecycler(Protocol):
    def request_served(self) -> None: ...


def create_sandbox_blueprint(
    engines: Mapping[SandboxOp, SandboxEngine],
    gate: SandboxGate,
    recycler: SandboxRecycler,
    limits_ceiling: SandboxLimits,
    config: Mapping[str, Any],
) -> Blueprint:
    """`config`: `work_root` (str), `engine_user` (name or None), `recycle` ("always" | "never").

    Raises ValueError for an unknown policy, or when the effective uid is 0 and the engine user is missing."""
    work_root = str(config["work_root"])
    policy = str(config.get("recycle", RECYCLE_ALWAYS))
    if policy not in (RECYCLE_ALWAYS, RECYCLE_NEVER):
        raise ValueError(f"unknown recycle policy {policy!r}")
    engine_ids = _resolve_engine_ids(config.get("engine_user"))

    bp = Blueprint("sandbox", __name__)
    state = {"busy": False, "draining": False}

    @bp.record_once
    def _apply_limits(setup_state: Any) -> None:
        cfg = setup_state.app.config
        cfg["MAX_CONTENT_LENGTH"] = REQUEST_CAP_BYTES
        cfg["BODY_TIMEOUT"] = max(CEILING_WALL_S, limits_ceiling.wall_s) + TIMEOUT_MARGIN_S
        cfg["RESPONSE_TIMEOUT"] = max(CEILING_WALL_S, limits_ceiling.wall_s) + TIMEOUT_MARGIN_S

    @bp.get("/health")
    async def health() -> Response:
        return Response("ok", status=200, content_type="text/plain")

    @bp.post("/v1/<op>")
    async def call(op: str) -> Response:
        started = time.monotonic()
        ctx = _RequestState(op=_UNKNOWN_OP, started=started)

        decision = gate.authorize(request.headers.get("Authorization"), op)
        if not decision.allowed:
            code = 403 if decision.reason == "forbidden_op" else 401
            return _refuse(ctx, code, SandboxStatus.ERROR, decision.reason or "unauthenticated")

        engine = _engine_for(engines, op)
        if engine is None:
            return _refuse(ctx, 404, SandboxStatus.BAD_REQUEST, "unknown_op")
        ctx.op = op

        if state["busy"] or state["draining"]:
            return _refuse(ctx, 503, SandboxStatus.RECYCLING, "recycling", close=True)
        state["busy"] = True
        handed_off = False
        try:
            declared = request.content_length
            if declared is not None and declared > REQUEST_CAP_BYTES:
                return _refuse(ctx, 413, SandboxStatus.TOO_LARGE, "request_too_large")

            try:
                meta = decode_meta(request.headers.get(META_HEADER, ""))
                limits = clamp_limits(meta.limits, limits_ceiling)
            except SandboxContractError:
                return _refuse(ctx, 400, SandboxStatus.BAD_REQUEST, "bad_meta")
            ctx.request_id = meta.request_id

            workdir = os.path.join(work_root, meta.request_id)
            try:
                os.mkdir(workdir, 0o700)
            except FileExistsError:
                return _refuse(ctx, 400, SandboxStatus.BAD_REQUEST, "bad_meta")
            ctx.workdir = workdir
            _chown(workdir, engine_ids)
            body_path = os.path.join(workdir, "body")
            fd = os.open(body_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            # From here on payload bytes exist (or are about to): this request ends the instance (G8).
            ctx.wrote = True
            ctx.recycles = policy == RECYCLE_ALWAYS
            if ctx.recycles:
                state["draining"] = True
            try:
                _chown(body_path, engine_ids)
                too_large = await _copy_body(fd, ctx)
            finally:
                os.close(fd)
            if too_large:
                handed_off = True
                return _answer_text(ctx, SandboxStatus.TOO_LARGE, "request_too_large", "request too large")

            try:
                result = await engine.run(body_path, workdir, limits)
            except Exception:
                logger.exception("[Sandbox] engine raised for op=%s", ctx.op)
                handed_off = True
                return _answer_text(ctx, SandboxStatus.ERROR, "", "engine failure")
            ctx.output_dir = os.path.dirname(result.output_path) if result.output_path else None
            ctx.peak_rss = int(result.peak_rss_bytes)

            handed_off = True
            return _answer_result(ctx, result)
        finally:
            _cleanup(ctx)
            if not handed_off:
                # Refused before any payload byte, or cancelled/failed before an answer existed.
                if not ctx.recycles:
                    state["busy"] = False
                _fire_recycler(ctx)
                _log(ctx, SandboxStatus.ERROR)

    def _refuse(ctx: "_RequestState", code: int, status: SandboxStatus, detail: str, *, close: bool = False
                ) -> Response:
        """An answer before any payload byte: no recycle (G8)."""
        resp = _text_response(code, status, detail, detail.replace("_", " "), ctx)
        if close:
            resp.headers["Connection"] = "close"
        _log(ctx, status)
        return resp

    def _answer_text(ctx: "_RequestState", status: SandboxStatus, header_detail: str, text: str) -> Response:
        resp = _text_response(_HTTP_STATUS[status], status, header_detail, text, ctx)
        if ctx.recycles:
            resp.headers["Connection"] = "close"
        resp.response = _EndBody(resp.response, lambda: _finish(ctx))
        _log(ctx, status)
        return resp

    def _answer_result(ctx: "_RequestState", result: SandboxEngineResult) -> Response:
        status = result.status
        if status == SandboxStatus.OK:
            opened = _open_output(result.output_path)
            if opened is None:
                logger.error("[Sandbox] engine reported ok without a usable output file (op=%s)", ctx.op)
                return _answer_text(ctx, SandboxStatus.ERROR, "", "engine failure")
            handle, size = opened
            if size > RESPONSE_CAP_BYTES:
                handle.close()
                return _answer_text(ctx, SandboxStatus.TOO_LARGE, "", "response too large")
            body = _EndBody(_FileChunks(handle), lambda: _finish(ctx))
            resp = Response(body, status=200, content_type="application/octet-stream")
            resp.headers["Content-Length"] = str(size)
            _attach_outcome(resp, ctx, SandboxStatus.OK, "")
            if ctx.recycles:
                resp.headers["Connection"] = "close"
            # Open file stays valid after the workdir and output directory are removed by the handler.
            _log(ctx, SandboxStatus.OK)
            return resp
        text = _bounded_text(result.detail)
        return _answer_text(ctx, status, "", text)

    def _finish(ctx: "_RequestState") -> None:
        """After the answer: free the instance for `never`, tell the recycler once for `always`."""
        if not ctx.recycles:
            state["busy"] = False
        _fire_recycler(ctx)

    def _fire_recycler(ctx: "_RequestState") -> None:
        if not ctx.recycles or ctx.recycled:
            return
        ctx.recycled = True
        try:
            recycler.request_served()
        except Exception:
            logger.exception("[Sandbox] recycler failed")

    async def _copy_body(fd: int, ctx: "_RequestState") -> bool:
        """Copy the request body to `fd`, counting bytes. True when the cap was crossed."""
        try:
            async for chunk in request.body:
                ctx.input_bytes += len(chunk)
                if ctx.input_bytes > REQUEST_CAP_BYTES:
                    return True
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
        except RequestEntityTooLarge:
            return True
        return False

    return bp


class _RequestState:
    """Per-request bookkeeping; private to this module."""

    def __init__(self, op: str, started: float) -> None:
        self.op = op
        self.started = started
        self.request_id = "-"
        self.workdir: Optional[str] = None
        self.output_dir: Optional[str] = None
        self.input_bytes = 0
        self.peak_rss = 0
        self.wrote = False
        self.recycles = False
        self.recycled = False
        self.logged = False


class _FileChunks:
    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[bytes]:
        while True:
            chunk = self._handle.read(_CHUNK)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._handle.close()


class _EndBody(ResponseBody):
    """Wraps a body and runs `on_end` when the server is done with it, even if it never iterated (client gone)."""

    def __init__(self, inner: Any, on_end: Any) -> None:
        self._inner = inner
        self._on_end = on_end
        self._done = False

    async def __aenter__(self) -> Any:
        if hasattr(self._inner, "__aenter__"):
            return await self._inner.__aenter__()
        return self._inner

    async def __aexit__(self, exc_type: Any, exc_value: Optional[BaseException], tb: Optional[TracebackType]) -> None:
        try:
            if hasattr(self._inner, "close"):
                self._inner.close()
            elif hasattr(self._inner, "__aexit__"):
                await self._inner.__aexit__(exc_type, exc_value, tb)
        finally:
            if not self._done:
                self._done = True
                self._on_end()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._inner.__aiter__()


def _engine_for(engines: Mapping[SandboxOp, SandboxEngine], op: str) -> Optional[SandboxEngine]:
    try:
        return engines.get(SandboxOp(op))
    except ValueError:
        return None


def _resolve_engine_ids(engine_user: Optional[str]) -> Optional[tuple]:
    if os.geteuid() != 0:
        return None
    if not engine_user:
        raise ValueError("running as root requires an engine user")
    try:
        entry = pwd.getpwnam(engine_user)
    except KeyError as exc:
        raise ValueError(f"engine user {engine_user!r} does not exist") from exc
    return entry.pw_uid, entry.pw_gid


def _chown(path: str, ids: Optional[tuple]) -> None:
    if ids is None:
        return
    os.chown(path, ids[0], ids[1])


def _bounded_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= ERROR_TEXT_CAP_BYTES:
        return text
    marker = _TRUNCATION_MARKER.encode()
    return raw[: ERROR_TEXT_CAP_BYTES - len(marker)].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _attach_outcome(resp: Response, ctx: "_RequestState", status: SandboxStatus, detail: str) -> None:
    stats = {
        "input_bytes": ctx.input_bytes,
        "wall_s": round(time.monotonic() - ctx.started, 3),
        "peak_rss_bytes": ctx.peak_rss,
    }
    resp.headers[OUTCOME_HEADER] = encode_outcome(SandboxOutcome(status=status, detail=detail, stats=stats))


def _text_response(code: int, status: SandboxStatus, header_detail: str, text: str,
                   ctx: "_RequestState") -> Response:
    resp = Response(_bounded_text(text).encode("utf-8"), status=code, content_type="text/plain; charset=utf-8")
    _attach_outcome(resp, ctx, status, header_detail)
    return resp


def _open_output(path: Optional[str]) -> Optional[tuple]:
    """Open the engine's server-owned output file; a regular file only, symlinks refused."""
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        logger.exception("[Sandbox] cannot open engine output")
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb"), info.st_size
    except OSError:
        os.close(fd)
        logger.exception("[Sandbox] cannot stat engine output")
        return None


def _cleanup(ctx: "_RequestState") -> None:
    for path in (ctx.workdir, ctx.output_dir):
        if not path:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            logger.error("[Sandbox] directory not removed: request_id=%s", ctx.request_id)


def _log(ctx: "_RequestState", status: Optional[SandboxStatus]) -> None:
    if status is None or ctx.logged:
        return
    ctx.logged = True
    logger.info(
        "[Sandbox] request_id=%s op=%s input_bytes=%d status=%s wall_s=%.3f peak_rss_bytes=%d recycled=%s",
        ctx.request_id, ctx.op, ctx.input_bytes, status.value, time.monotonic() - ctx.started,
        ctx.peak_rss, str(ctx.recycles).lower(),
    )
