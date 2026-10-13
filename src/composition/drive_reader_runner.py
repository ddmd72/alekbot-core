"""
DriveReaderRunner — the document reader agent as a ContentExtractorPort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.3, §24.8, §24.9).

Public interface:
    READER_VERSION = 1
    PdfSplitter (Protocol): async pdf_page_count(path) -> int
                            async cut_pdf_window(path, first_page, page_count, out_path) -> None
    DriveReaderRunner(context_builder, user_repo, prompt_builder, splitter, quota_service=None,
                      file_budget_s=MODEL_FILE_BUDGET_S, temp_root=None)
        extractor_id -> READER, version -> READER_VERSION
        async extract(request) -> ExtractionOutcome      raises DriveModelUnavailable only

Per file it reads the user's UserBotConfig, builds the agent through AgentContextBuilder (provider and tier are the
user's configuration, D7), sets the quota service and bills every call to `request.account_id` — the
CompanionExtractorRunner pattern. No prompt-content store: verbatim scans are not copied into BigQuery (§24.2).

A PDF's pages are counted by the sandboxed parser, then read in windows of MODEL_PAGES_PER_CALL pages, capped by the
month's remaining pages and MODEL_EXTRACT_MAX_PAGES; the indexer process never parses it. Windows run at most
MODEL_IN_FLIGHT at a time and join in page order. Each window is cut by the parser only when its turn comes, read into
memory and removed before the call, so at most MODEL_IN_FLIGHT window files exist on disk (a window can carry an image
every page shares). A window answered at the output limit, or whose call timed out, is cut in half from the source
and read once more; a half or a single page still at the limit adds no text and sets `truncated`, and a half that
times out again fails the file (`error`); a single-page window that times out is a model outage. A window the provider refuses as too large (HTTP 413) is halved down to one page; a
single page still too large fails the file. At
`file_budget_s` the windows done so far are kept and the rest cancelled (`truncated`). An image (the provider's
rendition) is one call. Outcomes: text; no_text (the reader found none); error (the provider refused an input, its
output stayed invalid, or the budget ran out before any window finished — never `timeout`, which means a parser
bomb); the parser's own status when it cannot count or cut the PDF (encrypted, timeout, memory, ...); budget_exhausted (no pages left this month); unsupported / too_large (an image the
providers do not take).
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Protocol

from ..agents.drive_document_reader_agent import AGENT_TYPE, DriveDocumentReaderAgent, ReaderReply
from ..infrastructure.drive_model_call import DriveInputTooLarge, DriveModelCallFailed, DriveModelTimedOut
from ..domain.agent import AgentConfig
from ..domain.drive_extraction import (
    READER, DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ExtractionStatus, PdfSplitFailed,
)
from ..domain.drive_image_mime import MAX_VISION_IMAGE_BYTES, VISION_MIME_TYPES, image_mime
from ..domain.drive_index import DocumentBrief
from ..domain.drive_index_params import (
    MODEL_EXTRACT_MAX_PAGES, MODEL_FILE_BUDGET_S, MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL,
)
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

READER_VERSION = 1
# The vision input cap of both reader providers, base64-safe (drive_image_mime); a bigger image is not sent.
MAX_IMAGE_BYTES = MAX_VISION_IMAGE_BYTES

S = ExtractionStatus


class PdfSplitter(Protocol):
    """The sandboxed parser's page-window ops (SubprocessContentExtractor); both raise PdfSplitFailed."""

    async def pdf_page_count(self, path: str) -> int: ...

    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str) -> None: ...


@dataclass
class _WindowRun:
    """What one window produced; each window task writes only its own (no state shared across tasks)."""
    first_page: int
    pages: int
    text: str = ""
    brief: Optional[DocumentBrief] = None
    finished: bool = False
    truncated: bool = False
    calls: int = 0
    cost: float = 0.0
    pages_sent: int = 0
    halves: int = 0


@dataclass
class _FileRun:
    windows: List[_WindowRun] = field(default_factory=list)

    def total(self, attr: str) -> Any:
        return sum(getattr(w, attr) for w in self.windows)


class DriveReaderRunner(ContentExtractorPort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 splitter: PdfSplitter, quota_service: Optional[QuotaService] = None,
                 file_budget_s: float = MODEL_FILE_BUDGET_S, temp_root: Optional[str] = None) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._splitter = splitter
        self._quota_service = quota_service
        self._file_budget_s = file_budget_s
        self._temp_root = temp_root

    @property
    def extractor_id(self) -> str:
        return READER

    @property
    def version(self) -> int:
        return READER_VERSION

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        if request.max_model_pages <= 0:
            return ExtractionOutcome(status=S.BUDGET_EXHAUSTED, detail="no reader pages left this month")
        if request.kind not in ("pdf", "image"):
            return ExtractionOutcome(status=S.UNSUPPORTED, detail=f"the reader takes no {request.kind}")
        agent = await self._agent(request.user_id)
        if request.kind == "image":
            outcome = await self._read_image(agent, request)
        else:
            outcome = await self._read_pdf(agent, request)
        logger.info(f"[DriveReader] user={request.user_id[:8]} kind={request.kind} status={outcome.status.value} "
                    f"pages={outcome.pages_sent}/{outcome.pages_total} calls={outcome.model_calls} "
                    f"cost=${outcome.model_cost:.4f} truncated={outcome.truncated} {outcome.detail}")
        return outcome

    # -- agent --------------------------------------------------------------------------------------------------

    async def _agent(self, user_id: str) -> DriveDocumentReaderAgent:
        """The reader with this user's provider, tier and thinking; DriveModelUnavailable when it cannot be built
        (no such user, a provider the indexer has no key for)."""
        user = await self._user_repo.get_user(user_id)
        if user is None:
            logger.error(f"[DriveReader] user {user_id[:8]} not found")
            raise DriveModelUnavailable("user not found")
        try:
            context = self._context_builder.build(AGENT_TYPE, user.config)
        except Exception as exc:
            logger.error(f"[DriveReader] cannot build the reader for user {user_id[:8]}: {type(exc).__name__}: {exc}")
            raise DriveModelUnavailable("reader provider unavailable") from exc
        agent = DriveDocumentReaderAgent(
            AgentConfig(agent_id=f"{AGENT_TYPE}_{user_id}", agent_type=AGENT_TYPE, capabilities=[AGENT_TYPE]),
            context, self._prompt_builder, thinking=user.config.get_thinking_for_agent(AGENT_TYPE))
        # Mirror UserAgentFactory's post-construction wiring: without it _flush_billing is a silent no-op.
        agent._quota_service = self._quota_service
        return agent

    # -- image --------------------------------------------------------------------------------------------------

    async def _read_image(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest) -> ExtractionOutcome:
        try:
            data = await asyncio.to_thread(Path(request.path).read_bytes)
        except OSError as exc:
            logger.error(f"[DriveReader] cannot read the image file: {exc}")
            return ExtractionOutcome(status=S.ERROR, pages_total=1, detail="image file unreadable")
        if len(data) > MAX_IMAGE_BYTES:
            return ExtractionOutcome(status=S.TOO_LARGE, pages_total=1, detail=f"image {len(data)} bytes")
        # Magic bytes first: a rendition is JPEG or PNG whatever the original's mime type (S0f).
        mime = image_mime(data[:16]) or request.mime_type
        if mime not in VISION_MIME_TYPES:
            return ExtractionOutcome(status=S.UNSUPPORTED, pages_total=1, detail=f"image type {mime or 'unknown'}")
        try:
            reply = await agent.read(data=data, mime_type=mime, file_name="image", first_window=True,
                                     account_id=request.account_id)
        except DriveModelCallFailed as exc:
            return ExtractionOutcome(status=S.ERROR, pages_total=1, model_calls=exc.model_calls,
                                     model_cost=exc.model_cost, detail=str(exc))
        return self._outcome(reply.text, reply.brief, pages_total=1, pages_sent=1, truncated=reply.output_limited,
                             calls=reply.calls, cost=reply.cost, timed_out=False, detail="image")

    # -- pdf ----------------------------------------------------------------------------------------------------

    async def _read_pdf(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest) -> ExtractionOutcome:
        cap = min(request.max_model_pages, MODEL_EXTRACT_MAX_PAGES)
        try:
            pages_total = await self._splitter.pdf_page_count(request.path)
        except PdfSplitFailed as exc:
            # the sandboxed parser's own verdict (encrypted, timeout, memory, ...) is the file's outcome (§24.3)
            logger.warning(f"[DriveReader] page count failed: {exc}")
            return ExtractionOutcome(status=exc.status, detail=f"split failed: {exc.detail}")
        except Exception as exc:
            logger.warning(f"[DriveReader] page count failed: {type(exc).__name__}: {exc}")
            return ExtractionOutcome(status=S.ERROR, detail=f"split failed: {type(exc).__name__}")
        if pages_total <= 0:
            return ExtractionOutcome(status=S.NO_TEXT, pages_total=max(pages_total, 0), detail="no pages")
        covered = min(pages_total, cap)
        run = _FileRun([_WindowRun(first_page=start, pages=min(MODEL_PAGES_PER_CALL, covered - start))
                        for start in range(0, covered, MODEL_PAGES_PER_CALL)])
        with tempfile.TemporaryDirectory(prefix="drive-reader-", dir=self._temp_root) as work:
            semaphore = asyncio.Semaphore(MODEL_IN_FLIGHT)
            tasks = [asyncio.create_task(self._window(agent, request, i, win, semaphore, work))
                     for i, win in enumerate(run.windows)]
            done, pending = await asyncio.wait(tasks, timeout=self._file_budget_s,
                                               return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        failures = [t.exception() for t in done if not t.cancelled() and t.exception() is not None]
        unavailable = next((e for e in failures if isinstance(e, DriveModelUnavailable)), None)
        if unavailable is not None:
            # what the windows answered before the outage is spent and billed: the tick counts it (§24.7, §24.9)
            raise DriveModelUnavailable(str(unavailable), pages_sent=run.total("pages_sent"),
                                        model_calls=run.total("calls"), model_cost=run.total("cost")) from unavailable
        if failures:
            first = failures[0]
            status, detail = S.ERROR, str(first)
            if isinstance(first, PdfSplitFailed):   # a window the parser could not cut: its status (§24.3)
                status, detail = first.status, f"split failed: {first.detail}"
            elif not isinstance(first, DriveModelCallFailed):
                logger.error(f"[DriveReader] window failed unexpectedly: {type(first).__name__}: {first}",
                             exc_info=first)
            return ExtractionOutcome(status=status, pages_total=pages_total, pages_sent=run.total("pages_sent"),
                                     model_calls=run.total("calls"), model_cost=run.total("cost"), detail=detail)
        finished = [w for w in run.windows if w.finished]
        cut = len(run.windows) - len(finished)
        if cut:
            logger.warning(f"[DriveReader] file budget {self._file_budget_s:.0f} s reached: {cut} of "
                           f"{len(run.windows)} windows not read")
        text = "\n\n".join(w.text for w in finished if w.text)
        return self._outcome(
            text, run.windows[0].brief, pages_total=pages_total, pages_sent=run.total("pages_sent"),
            truncated=pages_total > covered or bool(cut) or any(w.truncated for w in run.windows),
            calls=run.total("calls"), cost=run.total("cost"), timed_out=not finished,
            detail=f"windows={len(run.windows)} halves={run.total('halves')} cut={cut}")

    async def _window(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest, index: int,
                      win: _WindowRun, semaphore: asyncio.Semaphore, work: str) -> None:
        async with semaphore:
            texts = await self._piece(agent, request, work, index, win, win.first_page, win.pages,
                                      brief=index == 0, may_halve=True)
            win.text = "\n\n".join(t for t in texts if t)
            win.finished = True

    async def _piece(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest, work: str, index: int,
                     win: _WindowRun, first_page: int, pages: int, *, brief: bool, may_halve: bool) -> List[str]:
        """The text of pages [first_page, first_page + pages), in page order (§24.3):
        - output limit, or a timeout: halved once (`may_halve`); a half or a single page still at the limit adds no
          text and sets `truncated`; a half that times out again fails the file (`error`, a counted per-file retry);
          a single-page window that times out, never halved, is a model outage;
        - HTTP 413: halved, down to one page; a single page still too large fails the file (`error`).
        `brief`: this piece starts the document, so it asks for the brief (the first piece that answers sets it)."""
        try:
            reply = await self._call(agent, request, work, index, win, first_page, pages, first=brief)
        except DriveInputTooLarge:
            if pages <= 1:
                raise
            logger.warning(f"[DriveReader] window {index + 1} pages {first_page + 1}+{pages} too large (413); halved")
            return await self._halves(agent, request, work, index, win, first_page, pages, brief=brief,
                                      may_halve=may_halve)
        except DriveModelTimedOut as exc:
            if not may_halve:
                # already halved: the model answers, these pages are too dense for it — a per-file failure, counted
                # and capped by MAX_ATTEMPTS, never an outage that re-reads the finished windows (§8.2, T-M2)
                logger.warning(f"[DriveReader] window {index + 1} pages {first_page + 1}+{pages} timed out again "
                               f"after halving; the file fails")
                raise DriveModelCallFailed(f"pages {first_page + 1}+{pages} timed out after halving") from exc
            if pages <= 1:
                raise
            logger.warning(f"[DriveReader] window {index + 1} pages {first_page + 1}+{pages} timed out; halved")
            return await self._halves(agent, request, work, index, win, first_page, pages, brief=brief,
                                      may_halve=False)
        if not reply.output_limited:
            if brief and win.brief is None:
                win.brief = reply.brief
            return [reply.text]
        if pages <= 1 or not may_halve:
            logger.warning(f"[DriveReader] window {index + 1} pages {first_page + 1}+{pages} hit the output limit; "
                           f"not indexed")
            win.truncated = True
            return []
        return await self._halves(agent, request, work, index, win, first_page, pages, brief=brief, may_halve=False)

    async def _halves(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest, work: str, index: int,
                      win: _WindowRun, first_page: int, pages: int, *, brief: bool, may_halve: bool) -> List[str]:
        """Both halves of a piece, each cut from the source, read one after the other."""
        left = math.ceil(pages / 2)
        win.halves += 2
        texts = await self._piece(agent, request, work, index, win, first_page, left, brief=brief,
                                  may_halve=may_halve)
        texts += await self._piece(agent, request, work, index, win, first_page + left, pages - left, brief=False,
                                   may_halve=may_halve)
        return texts

    async def _cut(self, request: ExtractionRequest, work: str, first_page: int, pages: int) -> bytes:
        """One window, cut on demand from the source and removed once read: the call holds it in memory only."""
        out = os.path.join(work, f"pages-{first_page + 1:04d}-{pages:03d}.pdf")
        try:
            await self._splitter.cut_pdf_window(request.path, first_page, pages, out)
            return await asyncio.to_thread(Path(out).read_bytes)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(out)

    async def _call(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest, work: str, index: int,
                    win: _WindowRun, first_page: int, pages: int, first: bool) -> ReaderReply:
        """One call on pages [first_page, first_page + pages). Pages count once the model answered (§24.7); calls and
        cost as the agent's ledger reports them, also for a call that failed."""
        data = await self._cut(request, work, first_page, pages)
        try:
            reply = await agent.read(data=data, mime_type="application/pdf",
                                     file_name=f"window-{index + 1:03d}.pdf", first_window=first,
                                     account_id=request.account_id)
        except (DriveModelCallFailed, DriveModelUnavailable) as exc:
            win.calls += exc.model_calls
            win.cost += exc.model_cost
            raise
        win.calls += reply.calls
        win.cost += reply.cost
        win.pages_sent += pages
        return reply

    @staticmethod
    def _outcome(text: str, brief: Optional[DocumentBrief], *, pages_total: int, pages_sent: int, truncated: bool,
                 calls: int, cost: float, timed_out: bool, detail: str) -> ExtractionOutcome:
        if text.strip():
            return ExtractionOutcome(status=S.TEXT, text=text, brief=brief, pages_total=pages_total,
                                     pages_sent=pages_sent, truncated=truncated,
                                     extracted_by=f"{READER}@{READER_VERSION}", detail=detail, model_calls=calls,
                                     model_cost=cost)
        # Nothing read because the budget cut every window is not "the file has no text" (deterministic, §24.8),
        # nor a parser bomb (`timeout`): a per-file transient, retried with backoff (§8.2).
        return ExtractionOutcome(status=S.ERROR if timed_out else S.NO_TEXT, pages_total=pages_total,
                                 pages_sent=pages_sent, truncated=truncated, detail=detail, model_calls=calls,
                                 model_cost=cost)
