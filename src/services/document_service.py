"""
DocumentService — the document service of the main runtime (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, D1, D6, P13,
P21). One public class.

Bytes in, outcome out (P2): the open path stages a file and calls `read`; the service kind-checks the bytes, applies
the caps, decides native or pipeline (the parser in its child, the text-or-scan router, the reader within the
deadline) and returns a closed-schema `ReadOutcome`. It keeps nothing between calls and holds no store credential.

Its seams are ports, so the sandbox's parse slices swap adapters under it with no caller change (D14): the parser and
the reader are `ContentExtractorPort`s, the page operations a `PdfSplitterPort` (in the main service the parser adapter
serves both, `composition/document_service_setup.py`). The transcriber and the transcript cache serve P21's chat
audio; the quota service bills what the service itself spends.

`read` never raises: every path, a failure of a port included, returns an outcome, and every failure path logs one
line (ERROR for what is systemic or a bug, WARNING for what is the file's or the clock's) that the existing Cloud Run
severity alerting picks up (docs/07_deployment/ALERTING.md, policy 1). Logs carry the request id, kind and a failure
label, never document text or a file name.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from ..domain.billing import is_transcription_priced, transcription_cost, transcription_cost_known
from ..domain.document_extraction import PARSER, ExtractionStatus, chain_for, failure_label, model_route
from ..domain.document_kind import SNIFF_HEAD_BYTES, image_mime, kind_of, sniff_kind
from ..domain.document_limits import (
    MAX_FILE_BYTES, parse_input_cap, reader_text_budget, text_export_cap,
)
from ..domain.document_read import (
    CALLER_CONNECTOR, CALLER_INDEX, CALLER_PREFETCH, IN_PROCESS_TEXT_KINDS, PARSE_KINDS, READER_SKIPPED, TRANSCRIPT_CACHE_DAYS,
    ReadOutcome, ReadRequest, ReadStatus,
)
from ..domain.drive_extraction import (
    DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ParserBusy, ParserUnavailable, PdfSplitFailed,
)
from ..domain.drive_index_params import MODEL_EXTRACT_MAX_PAGES
from ..domain.native_format import NativeFormat
from ..domain.text_data_uris import drop_data_uri_images
from ..ports.audio_transcription_port import AudioTranscriptionPort
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.document_read_port import DocumentReadPort
from ..ports.pdf_splitter_port import PdfSplitterPort
from ..ports.quota_service import QuotaService
from ..ports.transcript_cache_port import TranscriptCachePort
from ..utils.logger import logger

_DEADLINE_GRACE_S = 1.0
_TEXT_LINK = "text-utf8"
_CACHE_LINK = "transcribe-cache"
_TRANSCRIBE_LINK = "transcribe-openai"
_EXT = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
# A `text/*` upload of these kinds is read raw (A3); csv, html, eml and subtitles go to the child's readers.
_RAW_TEXT_KINDS = IN_PROCESS_TEXT_KINDS | {"other", "data_dump", "mhtml"}
# ERROR: systemic or a bug (Errors-dev pages on it); everything else that is not a success is a WARNING.
_ERROR_STATUSES = frozenset({ReadStatus.ERROR, ReadStatus.MEMORY, ReadStatus.MODEL_UNAVAILABLE,
                             ReadStatus.PARSER_UNAVAILABLE})
# Callers that never run the paid model reader: the drive connector and the email index.
_NO_READER_CALLERS = frozenset({CALLER_CONNECTOR, CALLER_INDEX})
_QUIET_STATUSES = frozenset({ReadStatus.TEXT, ReadStatus.NATIVE, ReadStatus.NO_TEXT})


class DocumentService(DocumentReadPort):

    def __init__(self, parser: ContentExtractorPort, reader: ContentExtractorPort, splitter: PdfSplitterPort, *,
                 transcriber: Optional[AudioTranscriptionPort] = None,
                 transcript_cache: Optional[TranscriptCachePort] = None,
                 quota: Optional[QuotaService] = None) -> None:
        self._parser = parser
        self._reader = reader
        self._splitter = splitter
        self._transcriber = transcriber
        self._transcript_cache = transcript_cache
        self._quota = quota

    def allowance(self, user_id: str, account_id: str, store: str) -> int:
        """Pages the reader may read for one open (P13, D18): the reader's per-file ceiling `MODEL_EXTRACT_MAX_PAGES`
        for every store, the drive included. An interactive open books nothing against the drive's monthly reader
        pages, so `BUDGET_EXHAUSTED` is unreachable from the service. The one place a later budget replaces (D5)."""
        return MODEL_EXTRACT_MAX_PAGES

    async def read(self, request: ReadRequest) -> ReadOutcome:
        """See `DocumentReadPort.read`. Never raises; the whole read runs under the request's deadline."""
        remaining = request.deadline_at - time.time()
        if remaining <= 0:
            return self._done(request, "", ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline"))
        try:
            return await asyncio.wait_for(self._read(request), remaining + _DEADLINE_GRACE_S)
        except asyncio.TimeoutError:
            return self._done(request, "", ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline"))
        except Exception as exc:
            return self._done(request, "", ReadOutcome(status=ReadStatus.ERROR, detail=failure_label(exc)))

    # ----- the read, in order (RFC §4.4) ---------------------------------------------------------------------

    async def _read(self, request: ReadRequest) -> ReadOutcome:
        content = request.content
        declared = kind_of(request.name, request.declared_mime, False)
        kind = sniff_kind(content[:SNIFF_HEAD_BYTES], declared)
        try:
            outcome = await self._admit(request, declared, kind)
        except asyncio.TimeoutError:
            outcome = ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline")
        except Exception as exc:
            outcome = ReadOutcome(status=ReadStatus.ERROR, detail=failure_label(exc))
        return self._done(request, kind, outcome)

    async def _admit(self, request: ReadRequest, declared: str, kind: str) -> ReadOutcome:
        mime = request.declared_mime
        if kind == "audio":
            return await self._audio(request)
        if kind in ("pdf", "image"):
            return await self._pdf_or_image(request, kind)
        if kind in IN_PROCESS_TEXT_KINDS or (mime.lower().startswith("text/") and kind in _RAW_TEXT_KINDS):
            return await self._plain_text(request, kind)
        if kind in PARSE_KINDS:
            return await self._staged(request, kind, mime)
        return ReadOutcome(status=ReadStatus.UNSUPPORTED, detail=f"kind {kind}")

    async def _pdf_or_image(self, request: ReadRequest, kind: str) -> ReadOutcome:
        content = request.content
        native_mime = image_mime(content[:16]) if kind == "image" else "application/pdf"
        fmt = _native_format(request.native_formats, native_mime) if native_mime else None
        if fmt is not None and len(content) <= fmt.max_bytes:
            if kind == "image":
                return ReadOutcome(status=ReadStatus.NATIVE, native_mime=native_mime)
            return await self._native_pdf(request, fmt)
        if len(content) > parse_input_cap(request.store):
            return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{len(content)} bytes for kind {kind}")
        return await self._staged(request, kind, native_mime or request.declared_mime)

    async def _native_pdf(self, request: ReadRequest, fmt: NativeFormat) -> ReadOutcome:
        """P16: a PDF within the consumer's declaration goes native after the page count; encrypted or too many pages
        goes to the pipeline."""
        with tempfile.TemporaryDirectory(prefix="docsvc-") as workdir:
            path = await self._stage(request, workdir)
            try:
                pages = await self._splitter.pdf_page_count(path, deadline_at=request.deadline_at)
            except ParserBusy:
                return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
            except ParserUnavailable as exc:
                return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail=exc.reason)
            except PdfSplitFailed as exc:
                if exc.status is ExtractionStatus.ENCRYPTED:
                    pages = -1
                else:
                    return ReadOutcome(status=_status(exc.status), detail=exc.detail or exc.status.value)
        if pages < 0 or pages > fmt.max_pages:
            if len(request.content) > parse_input_cap(request.store):
                return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{len(request.content)} bytes for kind pdf")
            return await self._staged(request, "pdf", "application/pdf")
        return ReadOutcome(status=ReadStatus.NATIVE, native_mime=fmt.mime, pages_total=pages)

    async def _staged(self, request: ReadRequest, kind: str, mime: str) -> ReadOutcome:
        if kind in PARSE_KINDS and len(request.content) > parse_input_cap(request.store):
            return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{len(request.content)} bytes for kind {kind}")
        with tempfile.TemporaryDirectory(prefix="docsvc-") as workdir:
            path = await self._stage(request, workdir)
            return await self._pipeline(request, path, kind, mime)

    async def _stage(self, request: ReadRequest, workdir: str) -> str:
        ext = os.path.splitext(request.name)[1]
        path = os.path.join(workdir, "input" + (ext if _EXT.match(ext) else ""))

        def _write() -> None:
            with open(path, "wb") as handle:
                handle.write(request.content)

        await asyncio.to_thread(_write)
        return path

    # ----- the pipeline (step 5) -----------------------------------------------------------------------------

    async def _pipeline(self, request: ReadRequest, path: str, kind: str, mime: str) -> ReadOutcome:
        pages = self.allowance(request.user_id, request.account_id, request.store)
        chain = chain_for(kind)
        parsed: Optional[ExtractionOutcome] = None
        route: Optional[str] = None
        if chain[0] == PARSER:
            try:
                parsed = await self._parser.extract(ExtractionRequest(
                    path=path, name=request.name, kind=kind, mime_type=mime, user_id=request.user_id,
                    account_id=request.account_id, deadline_at=request.deadline_at))
            except ParserBusy:
                return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
            except ParserUnavailable as exc:
                return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail=exc.reason)
            route = model_route(parsed, kind)
            if route is None:
                return _from_extraction(parsed)
            if _busy(parsed):
                return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
        if request.caller in _NO_READER_CALLERS:
            # The drive connector and the email index never run the paid reader: what the router would hand it answers NO_TEXT, with
            # the parser's links and page count, and the route (or the reader-first kind) in the detail.
            return ReadOutcome(status=ReadStatus.NO_TEXT, links=_links(parsed) if parsed is not None else (),
                               pages_total=parsed.pages_total if parsed is not None else 0,
                               detail=f"{READER_SKIPPED}: {route or kind}")
        try:
            read = await self._reader.extract(ExtractionRequest(
                path=path, name=request.name, kind=kind, mime_type=mime, user_id=request.user_id,
                account_id=request.account_id, max_model_pages=pages, deadline_at=request.deadline_at,
                text_budget=reader_text_budget(request.store)))
        except ParserBusy:
            return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
        except ParserUnavailable as exc:   # the reader's page ops hit the sandbox; its spend so far is billed
            return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail=exc.reason, model_calls=exc.model_calls,
                               model_cost_usd=exc.model_cost)
        except DriveModelUnavailable as exc:
            if parsed is not None and parsed.status is ExtractionStatus.TEXT and route == "low_density":
                logger.warning(f"[DocumentService] reader unavailable for a low-density read; the parser's text is "
                               f"returned incomplete ({failure_label(exc)})")
                return ReadOutcome(status=ReadStatus.TEXT, text=parsed.text, pages_total=parsed.pages_total,
                                   truncated=parsed.truncated, may_be_incomplete=True,
                                   links=_links(parsed), detail="reader unavailable")
            return ReadOutcome(status=ReadStatus.MODEL_UNAVAILABLE, model_calls=exc.model_calls,
                               model_cost_usd=exc.model_cost, detail=failure_label(exc))
        if _busy(read):
            return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
        outcome = _from_extraction(read)
        links = _links(parsed, read) if parsed is not None else _links(read)
        calls = read.model_calls + (parsed.model_calls if parsed else 0)
        cost = read.model_cost + (parsed.model_cost if parsed else 0.0)
        pages_total = read.pages_total or (parsed.pages_total if parsed else 0)
        return _replace(outcome, links=links, model_calls=calls, model_cost_usd=cost, pages_total=pages_total)

    # ----- plain text in the caller (A3, G1b) -----------------------------------------------------------------

    async def _plain_text(self, request: ReadRequest, kind: str) -> ReadOutcome:
        if len(request.content) > text_export_cap():
            return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{len(request.content)} bytes for kind {kind}")

        def _decode() -> Tuple[str, int]:
            text = drop_data_uri_images(request.content.decode("utf-8", errors="replace"))
            return text, len(text.encode("utf-8"))

        text, size = await asyncio.to_thread(_decode)
        if size > MAX_FILE_BYTES:
            return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{size} bytes of text")
        if not text.strip():
            return ReadOutcome(status=ReadStatus.NO_TEXT, links=(_TEXT_LINK,))
        return ReadOutcome(status=ReadStatus.TEXT, text=text, links=(_TEXT_LINK,))

    # ----- audio (P21) ----------------------------------------------------------------------------------------

    async def _audio(self, request: ReadRequest) -> ReadOutcome:
        transcriber = self._transcriber
        if request.store != "chat" or request.caller == CALLER_PREFETCH or transcriber is None:
            return ReadOutcome(status=ReadStatus.UNSUPPORTED, detail="audio")
        if len(request.content) > MAX_FILE_BYTES:
            return ReadOutcome(status=ReadStatus.TOO_LARGE, detail=f"{len(request.content)} bytes for kind audio")
        key = hashlib.sha256(request.content).hexdigest() + ":" + str(getattr(transcriber, "model", ""))
        cached = await self._cache_get(request, key)
        if cached is not None:
            return ReadOutcome(status=ReadStatus.TEXT, text=cached, links=(_CACHE_LINK,))
        remaining = request.deadline_at - time.time()
        if remaining <= 0:
            return ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline")
        with tempfile.TemporaryDirectory(prefix="docsvc-") as workdir:
            path = await self._stage(request, workdir)
            try:
                transcript = await asyncio.wait_for(
                    transcriber.transcribe_metered(path, _audio_mime(request)), remaining)
            except asyncio.TimeoutError:
                return ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline")
            except Exception as exc:
                return ReadOutcome(status=ReadStatus.ERROR, detail=f"transcription: {failure_label(exc)}")
        cost = transcription_cost(transcript)
        if not transcription_cost_known(transcript):
            reason = "no published rate for its usage" if is_transcription_priced(transcript.model) else "unpriced model"
            logger.warning(f"[DocumentService] transcription billed at 0.0: {reason} (request {request.request_id})")
        await self._bill(request, transcript.model, transcript.input_tokens + transcript.output_tokens, cost)
        if not transcript.text.strip():
            return ReadOutcome(status=ReadStatus.NO_TEXT, links=(_TRANSCRIBE_LINK,), model_calls=1,
                               model_cost_usd=cost)
        await self._cache_put(request, key, transcript.text)
        return ReadOutcome(status=ReadStatus.TEXT, text=transcript.text, links=(_TRANSCRIBE_LINK,), model_calls=1,
                           model_cost_usd=cost)

    async def _cache_get(self, request: ReadRequest, key: str) -> Optional[str]:
        if self._transcript_cache is None:
            return None
        try:
            return await self._transcript_cache.get(request.account_id, key)
        except Exception as exc:
            logger.warning(f"[DocumentService] transcript cache read failed ({failure_label(exc)}); transcribing")
            return None

    async def _cache_put(self, request: ReadRequest, key: str, text: str) -> None:
        if self._transcript_cache is None:
            return
        try:
            purge_after = datetime.now(timezone.utc) + timedelta(days=TRANSCRIPT_CACHE_DAYS)
            await self._transcript_cache.put(request.account_id, key, text, purge_after=purge_after)
        except Exception as exc:
            logger.warning(f"[DocumentService] transcript cache write failed ({failure_label(exc)})")

    async def _bill(self, request: ReadRequest, model: str, tokens: int, cost: float) -> None:
        if self._quota is None:
            logger.error(f"[DocumentService] no quota service: a transcription of {cost} USD is not booked "
                         f"(request {request.request_id})")
            return
        try:
            await self._quota.record_usage(request.account_id, model, tokens, cost)
        except Exception as exc:
            logger.error(f"[DocumentService] booking a transcription failed ({failure_label(exc)}); "
                         f"{cost} USD is lost (request {request.request_id})")

    # ----- outcome and its log line (step 8) ------------------------------------------------------------------

    def _done(self, request: ReadRequest, kind: str, outcome: ReadOutcome) -> ReadOutcome:
        """The one exit: stamps the sniffed kind and logs every failure path once, at the severity the alerting
        reads (ERROR: systemic or a bug; WARNING: the file's or the clock's). No document text, no file name."""
        if kind and not outcome.kind:
            outcome = _replace(outcome, kind=kind)
        if outcome.status not in _QUIET_STATUSES:
            line = (f"[DocumentService] read {outcome.status.value}: kind={outcome.kind or '?'} "
                    f"store={request.store} caller={request.caller} detail={outcome.detail[:120]!r} "
                    f"request={request.request_id}")
            if outcome.status in _ERROR_STATUSES:
                logger.error(line)
            else:
                logger.warning(line)
        return outcome


def _replace(outcome: ReadOutcome, **changes: object) -> ReadOutcome:
    return replace(outcome, **changes)  # type: ignore[arg-type]


def _status(status: ExtractionStatus) -> ReadStatus:
    if status is ExtractionStatus.BUDGET_EXHAUSTED:    # P13: cannot occur; never a silent success
        return ReadStatus.ERROR
    return ReadStatus(status.value)


def _busy(outcome: ExtractionOutcome) -> bool:
    return outcome.status is ExtractionStatus.TIMEOUT and outcome.detail == "busy"


def _links(*outcomes: ExtractionOutcome) -> Tuple[str, ...]:
    return tuple(o.extracted_by for o in outcomes if o.extracted_by)


def _from_extraction(outcome: ExtractionOutcome) -> ReadOutcome:
    if _busy(outcome):
        return ReadOutcome(status=ReadStatus.PARSER_UNAVAILABLE, detail="busy")
    status = _status(outcome.status)
    detail = outcome.detail if status is not ReadStatus.TEXT else ""
    if outcome.status is ExtractionStatus.BUDGET_EXHAUSTED:
        detail = "budget exhausted (unreachable)"
    return ReadOutcome(
        status=status, text=outcome.text, pages_total=outcome.pages_total, pages_read=outcome.pages_read,
        skipped_pages=outcome.skipped_pages, truncated=outcome.truncated, links=_links(outcome),
        model_calls=outcome.model_calls, model_cost_usd=outcome.model_cost, detail=detail,
        cut_within_pages=outcome.cut_within_pages)


def _native_format(formats: Tuple[NativeFormat, ...], mime: str) -> Optional[NativeFormat]:
    for fmt in formats:
        if fmt.mime == mime:
            return fmt
    return None


def _audio_mime(request: ReadRequest) -> str:
    declared = request.declared_mime.split(";", 1)[0].strip().lower()
    if declared.startswith("audio/") or declared == "video/mp4":
        return declared
    return mimetypes.guess_type(request.name)[0] or "audio/mpeg"
