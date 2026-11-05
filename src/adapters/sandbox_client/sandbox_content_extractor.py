"""
SandboxContentExtractor — the HTTP side of the parser: `ContentExtractorPort` and `PdfSplitterPort` over the sandbox's
light service (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.7 `parse.*` ops, §4.9, §4.10).

It decides exactly where `SubprocessContentExtractor` decides (the stat and the per-kind size cap before any upload, the
result checks and their mapping, the page ops' failures) and differs only in where the parser runs. The request
carries the file as its raw body and only `kind`, `ext` (the extension of the request's name, lowercased) and the
limits; no user, account, item or file name (I3). A `ok` answer holds the parser's own verdict (header detail and the
JSON body's `status`); any other answer is a run failure, and each failure ends as an outcome the caller turns into a
note, plus a log line (the client logs the summary at WARNING; an engine `bad_request` / `script_error` / `empty_output`
is our bug and logs at ERROR here).

An outage never becomes a file's outcome: `SandboxUnavailable` / `SandboxMisconfigured` end as
`drive_extraction.ParserUnavailable` (busy | unavailable | misconfigured), systemic, `charges_file=False`. `ParserBusy`
is never raised: there is no client-side slot (D1); the service's own 429 is `ParserUnavailable("busy")`.

`cut_pdf_windows` is one `parse.pdf_windows` request (a window is a batch of one); the answer is a frame split by the
domain's `split_windows`, all or nothing. Nothing here imports a parser library, `json` or `decode` (architecture
test); bodies are decoded by `domain/sandbox_parse_result` and `domain/sandbox_windows_frame`.

`container_facts` (`ContainerFactsPort`, EXTERNAL_FILES D13 / owner decision O2) is one `parse.container_facts`
request with the bytes as the body: the ZIP facts the D13 macro check needs, read in the sandbox. A run failure or an
invalid answer is `ContainerFacts(status=ERROR)`; an outage is `ParserUnavailable`, as for the other ops.

Public interface:
    SandboxContentExtractor(client)    extractor_id = PARSER, version = PARSER_VERSION
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...domain.container_facts import ContainerFacts, ContainerStatus, container_facts_from_result
from ...domain.document_extraction import PARSER, PARSER_VERSION, ExtractionStatus
from ...domain.drive_capture import signals_from_json
from ...domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ParserUnavailable, PdfSplitFailed
from ...domain.drive_index_params import MAX_TEXT_CHARS
from ...domain.drive_index_policy import size_cap
from ...domain.sandbox_contract import (
    CEILING_CPU_S,
    CEILING_FILE_BYTES,
    CEILING_MEMORY_BYTES,
    CEILING_WALL_S,
    REQUEST_CAP_BYTES,
    SANDBOX_MAX_WINDOWS,
    SandboxLimits,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
)
from ...domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from ...domain.sandbox_parse_result import ParseResultError, decode_parse_result
from ...domain.sandbox_windows_frame import BadFrame, split_windows
from ...ports.container_facts_port import ContainerFactsPort
from ...ports.content_extractor_port import ContentExtractorPort
from ...ports.pdf_splitter_port import PdfSplitterPort
from ...utils.logger import logger
from .sandbox_http_client import SandboxHttpClient, SandboxReply

_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")
_CHILD_STATUSES = frozenset({ExtractionStatus.TEXT, ExtractionStatus.NO_TEXT, ExtractionStatus.UNSUPPORTED,
                             ExtractionStatus.TOO_LARGE, ExtractionStatus.ENCRYPTED, ExtractionStatus.ERROR})
_DETAIL_MAX = 300
# Engine statuses that are a bug of ours, never the file's: logged at ERROR.
_OUR_BUG = frozenset({SandboxStatus.BAD_REQUEST, SandboxStatus.SCRIPT_ERROR, SandboxStatus.EMPTY_OUTPUT,
                      SandboxStatus.RECYCLING, SandboxStatus.UNCONFIGURED})
_RUN_FAILURES = {
    SandboxStatus.TIMEOUT: ExtractionStatus.TIMEOUT,
    SandboxStatus.MEMORY: ExtractionStatus.MEMORY,
    SandboxStatus.TOO_LARGE: ExtractionStatus.TOO_LARGE,
    SandboxStatus.ERROR: ExtractionStatus.ERROR,
}


class SandboxContentExtractor(ContentExtractorPort, PdfSplitterPort, ContainerFactsPort):

    def __init__(self, client: SandboxHttpClient) -> None:
        self._client = client

    @property
    def extractor_id(self) -> str:
        return PARSER

    @property
    def version(self) -> int:
        return PARSER_VERSION

    @property
    def stamp(self) -> str:
        return f"{PARSER}@{PARSER_VERSION}"

    # -- ContentExtractorPort -----------------------------------------------------------------------------------

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        try:
            size = os.path.getsize(request.path)
        except OSError as exc:
            logger.warning(f"[SandboxParse] cannot stat the downloaded file: {type(exc).__name__}")
            return self._outcome(ExtractionStatus.ERROR, detail=f"stat: {type(exc).__name__}")
        if size > size_cap(request.kind) or size > REQUEST_CAP_BYTES:
            return self._outcome(ExtractionStatus.TOO_LARGE, detail=f"{size} bytes for kind {request.kind}")
        limits, lowered = _limits(request.deadline_at)
        if limits is None:
            logger.warning("[SandboxParse] extract: the deadline passed, no request sent")
            return self._outcome(ExtractionStatus.TIMEOUT, detail="deadline")
        name_ext = os.path.splitext(request.name)[1].lstrip(".").lower()
        ext = name_ext if _EXT_RE.fullmatch(name_ext) else None
        reply = await self._call(SandboxOp.PARSE_EXTRACT, request.path, limits, request.deadline_at,
                                 kind=request.kind, ext=ext)
        failure = self._run_failure(SandboxOp.PARSE_EXTRACT, reply.outcome, lowered)
        if failure is not None:
            return self._outcome(failure[0], detail=failure[1])
        result = self._result(SandboxOp.PARSE_EXTRACT, reply)
        if result is None:
            return self._outcome(ExtractionStatus.ERROR, detail="invalid result")
        return self._from_result(result)

    # -- PdfSplitterPort ----------------------------------------------------------------------------------------

    async def pdf_page_count(self, path: str, *, deadline_at: Optional[float] = None) -> int:
        result = await self._page_op(SandboxOp.PARSE_PDF_PAGE_COUNT, path, deadline_at, None)
        pages = result.get("pages_total")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            logger.warning("[SandboxParse] page count result has an invalid shape")
            raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid page count")
        return pages

    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str,
                             *, deadline_at: Optional[float] = None) -> None:
        await self.cut_pdf_windows(path, [(first_page, page_count)], [out_path], deadline_at=deadline_at)

    async def cut_pdf_windows(self, path: str, windows: Sequence[Tuple[int, int]], out_paths: Sequence[str],
                              *, deadline_at: Optional[float] = None) -> None:
        """Every window at its out path from one request. All or nothing: on any failure or cancellation no window
        file of the batch is left."""
        if len(windows) != len(out_paths):
            logger.error(f"[SandboxParse] cut_pdf_windows: {len(windows)} windows for {len(out_paths)} out paths")
            raise ValueError("windows and out_paths differ in length")
        if not windows:
            return
        if len(windows) > SANDBOX_MAX_WINDOWS:
            logger.error(f"[SandboxParse] cut_pdf_windows: {len(windows)} windows exceed {SANDBOX_MAX_WINDOWS}")
            raise PdfSplitFailed(ExtractionStatus.ERROR, "too many windows")
        written: List[str] = []
        try:
            frame = await self._page_op(SandboxOp.PARSE_PDF_WINDOWS, path, deadline_at, windows)
            parts = frame["windows"]
            if len(parts) != len(windows):
                logger.warning("[SandboxParse] the window frame holds a different number of windows than asked")
                raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid window frame")
            for part, out_path in zip(parts, out_paths):
                written.append(out_path)
                await asyncio.to_thread(_write, out_path, part)
        except BaseException as exc:
            logger.warning(f"[SandboxParse] cut_pdf_windows failed ({type(exc).__name__}); removing the batch's files")
            for out_path in written:
                with contextlib.suppress(OSError):
                    os.unlink(out_path)
            raise

    # -- ContainerFactsPort -------------------------------------------------------------------------------------

    async def container_facts(self, data: bytes, *, deadline_at: Optional[float] = None) -> ContainerFacts:
        op = SandboxOp.PARSE_CONTAINER_FACTS
        if len(data) > REQUEST_CAP_BYTES:
            return ContainerFacts(status=ContainerStatus.TOO_LARGE, detail=f"{len(data)} bytes")
        limits, lowered = _limits(deadline_at)
        if limits is None:
            logger.warning("[SandboxParse] container_facts: the deadline passed, no request sent")
            return ContainerFacts(status=ContainerStatus.ERROR, detail="deadline")
        reply = await self._call(op, None, limits, deadline_at, body=data)
        failure = self._run_failure(op, reply.outcome, lowered)
        if failure is not None:
            return ContainerFacts(status=ContainerStatus.ERROR, detail=f"{failure[0].value}: {failure[1]}")
        result = self._result(op, reply)
        if result is None:
            return ContainerFacts(status=ContainerStatus.ERROR, detail="invalid result")
        return container_facts_from_result(result)

    # -- the call -----------------------------------------------------------------------------------------------

    async def _call(self, op: SandboxOp, path: Optional[str], limits: SandboxLimits, deadline_at: Optional[float], *,
                    kind: Optional[str] = None, ext: Optional[str] = None,
                    windows: Optional[Sequence[Tuple[int, int]]] = None, body: Optional[bytes] = None) -> SandboxReply:
        try:
            return await self._client.call(op, body, limits, kind=kind, ext=ext, body_path=path,
                                           deadline_at=deadline_at, windows=windows)
        except SandboxUnavailable as exc:
            raise ParserUnavailable(exc.reason) from exc
        except SandboxMisconfigured as exc:
            raise ParserUnavailable("misconfigured") from exc

    async def _page_op(self, op: SandboxOp, path: str, deadline_at: Optional[float],
                       windows: Optional[Sequence[Tuple[int, int]]]) -> Dict[str, Any]:
        """The page op's validated result; for `pdf_windows` with `text` the result is `{"windows": [bytes...]}`.
        Raises PdfSplitFailed with the outcome status for every other verdict."""
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            logger.warning(f"[SandboxParse] cannot stat the PDF: {type(exc).__name__}")
            raise PdfSplitFailed(ExtractionStatus.ERROR, f"stat: {type(exc).__name__}") from exc
        if size > size_cap("pdf") or size > REQUEST_CAP_BYTES:
            raise PdfSplitFailed(ExtractionStatus.TOO_LARGE, f"{size} bytes for kind pdf")
        limits, lowered = _limits(deadline_at)
        if limits is None:
            logger.warning(f"[SandboxParse] {op.value}: the deadline passed, no request sent")
            raise PdfSplitFailed(ExtractionStatus.TIMEOUT, "deadline")
        reply = await self._call(op, path, limits, deadline_at, ext="pdf", windows=windows)
        failure = self._run_failure(op, reply.outcome, lowered)
        if failure is not None:
            raise PdfSplitFailed(failure[0], failure[1])
        if op is SandboxOp.PARSE_PDF_WINDOWS and reply.outcome.detail == ExtractionStatus.TEXT.value:
            try:
                return {"windows": split_windows(reply.body)}
            except BadFrame as exc:
                logger.warning("[SandboxParse] the window frame is malformed")
                raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid window frame") from exc
        result = self._result(op, reply)
        if result is None:
            raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid result")
        if result["status"] != ExtractionStatus.TEXT.value:
            raise PdfSplitFailed(ExtractionStatus(result["status"]), str(result.get("detail", ""))[:_DETAIL_MAX])
        return result

    def _run_failure(self, op: SandboxOp, outcome: SandboxOutcome,
                     lowered: bool) -> Optional[Tuple[ExtractionStatus, str]]:
        """(status, detail) of a run failure (a non-ok answer), or None for `ok`."""
        status = outcome.status
        if status is SandboxStatus.OK:
            return None
        if status in _OUR_BUG:
            logger.error(f"[SandboxParse] {op.value}: the service refused our request ({status.value}); this is a bug")
            return ExtractionStatus.ERROR, status.value
        mapped = _RUN_FAILURES.get(status, ExtractionStatus.ERROR)
        detail = outcome.detail[:_DETAIL_MAX]
        if mapped is ExtractionStatus.TIMEOUT and lowered:
            detail = "deadline"   # the wall was lowered to the caller's deadline: the clock's, not the file's
        return mapped, detail

    def _result(self, op: SandboxOp, reply: SandboxReply) -> Optional[Dict[str, Any]]:
        """The decoded body of an `ok` answer, or None (and a WARNING) when it is invalid or contradicts the header."""
        try:
            result = decode_parse_result(reply.body, op)
        except ParseResultError:
            logger.warning(f"[SandboxParse] {op.value}: the result body is invalid")
            return None
        if result["status"] != reply.outcome.detail:
            logger.warning(f"[SandboxParse] {op.value}: the result status differs from the header's")
            return None
        return result

    # -- results (the in-process adapter's mapping, mirrored) --------------------------------------------------

    def _outcome(self, status: ExtractionStatus, *, detail: str = "") -> ExtractionOutcome:
        return ExtractionOutcome(status=status, extracted_by=self.stamp, detail=detail)

    def _from_result(self, result: Dict[str, Any]) -> ExtractionOutcome:
        try:
            status = ExtractionStatus(result["status"])
            text = result.get("text", "")
            pages_total = result.get("pages_total", 0)
            truncated = result.get("truncated", False)
            script_rendered = result.get("script_rendered", False)
            if (status not in _CHILD_STATUSES or not isinstance(text, str) or not isinstance(pages_total, int)
                    or pages_total < 0 or not isinstance(truncated, bool) or not isinstance(script_rendered, bool)):
                raise ValueError("bad field")
        except (KeyError, ValueError, TypeError):
            logger.warning("[SandboxParse] the result has an invalid shape")
            return self._outcome(ExtractionStatus.ERROR, detail="invalid result")
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
        if status is ExtractionStatus.TEXT and not text.strip():
            status = ExtractionStatus.NO_TEXT
        is_text = status is ExtractionStatus.TEXT
        signals = signals_from_json(result["capture_signals"]) if "capture_signals" in result else None
        if "capture_signals" in result and signals is None:
            logger.warning("[SandboxParse] the capture signals have an invalid shape; dropped")
        return ExtractionOutcome(status=status, text=text if is_text else "", pages_total=pages_total,
                                 truncated=truncated, extracted_by=self.stamp,
                                 detail=str(result.get("detail", ""))[:_DETAIL_MAX],
                                 script_rendered=is_text and script_rendered, capture_signals=signals)


def _limits(deadline_at: Optional[float]) -> Tuple[Optional[SandboxLimits], bool]:
    """(limits, lowered): the service's ceilings with the wall lowered to the time left when that is smaller; None
    when no time is left."""
    wall = CEILING_WALL_S
    lowered = False
    if deadline_at is not None:
        left = deadline_at - time.time()
        if left <= 0:
            return None, True
        if left < wall:
            wall, lowered = left, True
    return SandboxLimits(wall_s=wall, memory_bytes=CEILING_MEMORY_BYTES, cpu_s=CEILING_CPU_S,
                         file_bytes=CEILING_FILE_BYTES), lowered


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)
