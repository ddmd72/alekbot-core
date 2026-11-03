"""
DriveReaderRunner — the document reader agent as a ContentExtractorPort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.3, §24.8, §24.9).

Public interface:
    READER_VERSION = 2
    DriveReaderRunner(context_builder, user_repo, prompt_builder, splitter, quota_service=None,
                      file_budget_s=MODEL_FILE_BUDGET_S, temp_root=None)
        extractor_id -> READER, version -> READER_VERSION
        async extract(request) -> ExtractionOutcome      raises DriveModelUnavailable only
    `splitter` is a PdfSplitterPort (ports/pdf_splitter_port.py); the runner calls only pdf_page_count and
    cut_pdf_window, without a deadline unless the request carries `deadline_at`. A request with `deadline_at` or
    `text_budget` (an interactive open, EXTERNAL_FILES_RFC §4.4) is read by `_read_pdf_limited`: one window at a time
    in page order, stopping at the budget or the deadline, the finished prefix kept; the indexer sets neither and
    reads as before.

Per file it reads the user's UserBotConfig, builds the agent through AgentContextBuilder (provider and tier are the
user's configuration, D7), sets the quota service and bills every call to `request.account_id` — the
CompanionExtractorRunner pattern. No prompt-content store: verbatim scans are not copied into BigQuery (§24.2).

A PDF's pages are counted by the sandboxed parser, then read in windows of MODEL_PAGES_PER_CALL pages, capped by the
month's remaining pages and MODEL_EXTRACT_MAX_PAGES; the indexer process never parses it. Windows run at most
MODEL_IN_FLIGHT at a time and join in page order. Each window is cut by the parser only when its turn comes, read into
memory and removed before the call, so at most MODEL_IN_FLIGHT window files exist on disk (a window can carry an image
every page shares). A window answered at the output limit, or whose call timed out, is cut in half from the source
and read once more; a half or a single page still at the limit adds no text and sets `truncated`, and a half that
times out again fails the file (`error`); a single-page window that times out is a model outage. A window the provider
refuses (HTTP 413, 400 or 422) is halved down to one page; a single page still refused is skipped and the rest of the
file is read (`truncated`, `pages_refused`, R3-P1). At
`file_budget_s` the windows done so far are kept and the rest cancelled (`truncated`). An image (the provider's
rendition) is one call. Outcomes: text; no_text (the reader found none); too_large / unsupported (terminal, never
retried: every page of the PDF refused, or the image refused — too_large when a refusal was 413; or an image the
providers do not take); error (output stayed invalid, a refusal
raised before any HTTP exchange, or the budget cut a window while no window found text — never `timeout`, which
means a parser bomb); the parser's own status when it cannot count or cut the PDF (encrypted, timeout, memory, ...);
budget_exhausted (no pages left this month).
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, List, Optional

from ..agents.drive_document_reader_agent import AGENT_TYPE, DriveDocumentReaderAgent, ReaderReply
from ..infrastructure.drive_model_call import (
    DriveInputRefused, DriveInputTooLarge, DriveModelCallFailed, DriveModelTimedOut,
)
from ..domain.agent import AgentConfig
from ..domain.drive_extraction import (
    DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ParserBusy, ParserUnavailable,
    PdfSplitFailed,
)
from ..domain.document_extraction import READER, ExtractionStatus
from ..domain.drive_image_mime import MAX_VISION_IMAGE_BYTES, VISION_MIME_TYPES
from ..domain.document_kind import image_mime
from ..domain.drive_index import DocumentBrief
from ..domain.drive_open_cut import head_at_paragraph
from ..domain.drive_index_params import (
    MODEL_EXTRACT_MAX_PAGES, MODEL_FILE_BUDGET_S, MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL,
)
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.pdf_splitter_port import PdfSplitterPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

READER_VERSION = 2   # 2: `capture`, no brief doc type (§26.3, Δ64)
# The vision input cap of both reader providers, base64-safe (drive_image_mime); a bigger image is not sent.
MAX_IMAGE_BYTES = MAX_VISION_IMAGE_BYTES

S = ExtractionStatus


@dataclass
class _WindowRun:
    """What one window produced; each window task writes only its own (no state shared across tasks)."""
    first_page: int
    pages: int
    text: str = ""
    brief: Optional[DocumentBrief] = None
    captures: List[str] = field(default_factory=list)   # what this window's kept pieces said of the capture (§26.3)
    finished: bool = False
    truncated: bool = False
    calls: int = 0
    cost: float = 0.0
    fallback_cost: float = 0.0   # of `cost`, what the fallback served (the fallback alert, XC-5)
    pages_sent: int = 0
    halves: int = 0
    refused: int = 0             # single pages the provider refused, skipped (R3-P1)
    refused_too_large: int = 0   # of them, refused as too large (413)
    skipped: List[int] = field(default_factory=list)   # 1-based pages refused or cut by the output limit (D7)


@dataclass
class _FileRun:
    windows: List[_WindowRun] = field(default_factory=list)

    def total(self, attr: str) -> Any:
        return sum(getattr(w, attr) for w in self.windows)


class DriveReaderRunner(ContentExtractorPort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 splitter: PdfSplitterPort, quota_service: Optional[QuotaService] = None,
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
        logger.info(f"[DriveReader] user={request.user_id[:8]} item={request.item_id} kind={request.kind} status={outcome.status.value} "
                    f"pages={outcome.pages_sent}/{outcome.pages_total} calls={outcome.model_calls} "
                    f"cost=${outcome.model_cost:.4f} truncated={outcome.truncated} refused={outcome.pages_refused} "
                    f"{outcome.detail}")
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
            read = agent.read(data=data, mime_type=mime, file_name="image", first_window=True,
                              account_id=request.account_id)
            if request.deadline_at is None:
                reply = await read
            else:
                # an interactive open (§4.4): the call runs only in the time left
                remaining = request.deadline_at - time.time()
                if remaining <= 0:
                    read.close()
                    logger.warning(f"[DriveReader] item={request.item_id}: the deadline passed, the image was not read")
                    return ExtractionOutcome(status=S.TIMEOUT, pages_total=1, detail="deadline")
                reply = await asyncio.wait_for(read, remaining)
        except asyncio.TimeoutError:
            logger.warning(f"[DriveReader] item={request.item_id}: the image read hit the deadline")
            return ExtractionOutcome(status=S.TIMEOUT, pages_total=1, detail="deadline")
        except DriveModelCallFailed as exc:
            return ExtractionOutcome(status=_failed_status(exc), pages_total=1, model_calls=exc.model_calls,
                                     model_cost=exc.model_cost,
                                     model_fallback_cost=getattr(exc, "model_fallback_cost", 0.0), detail=str(exc))
        return self._outcome(reply.text, reply.brief, pages_total=1, pages_sent=1, truncated=reply.output_limited,
                             calls=reply.calls, cost=reply.cost, fallback_cost=reply.fallback_cost, timed_out=False,
                             detail="image", capture=reply.capture)

    # -- pdf ----------------------------------------------------------------------------------------------------

    async def _read_pdf(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest) -> ExtractionOutcome:
        cap = min(request.max_model_pages, MODEL_EXTRACT_MAX_PAGES)
        limited = request.text_budget is not None or request.deadline_at is not None
        try:
            pages_total = await (self._splitter.pdf_page_count(request.path, deadline_at=request.deadline_at)
                                 if request.deadline_at is not None else self._splitter.pdf_page_count(request.path))
        except ParserUnavailable as exc:
            # the sandbox cannot take the call: systemic, never the file's outcome (the document service answers the
            # unavailable note, the indexer ends its tick)
            logger.warning(f"[DriveReader] page count: the parser is unavailable ({exc.reason})")
            raise
        except ParserBusy:
            # the slot did not come free before the deadline: raised, not turned into a `split failed` outcome, so
            # the document service answers the busy note (an outcome of timeout/"split failed: busy" matched nothing)
            logger.warning("[DriveReader] page count: the parser is busy")
            raise
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
        if limited:
            return await self._read_pdf_limited(agent, request, run, pages_total, covered)
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
                                        model_calls=run.total("calls"), model_cost=run.total("cost"),
                                        model_fallback_cost=run.total("fallback_cost"),
                                        charges_file=unavailable.charges_file) from unavailable
        parser_down = next((e for e in failures if isinstance(e, ParserUnavailable)), None)
        if parser_down is not None:
            # the sandbox could not cut a window: systemic; what the other windows spent is billed (never an outcome)
            raise ParserUnavailable(parser_down.reason, pages_sent=run.total("pages_sent"),
                                    model_calls=run.total("calls"), model_cost=run.total("cost"),
                                    model_fallback_cost=run.total("fallback_cost")) from parser_down
        if failures:
            first = failures[0]
            status, detail = S.ERROR, str(first)
            if isinstance(first, PdfSplitFailed):   # a window the parser could not cut: its status (§24.3)
                status, detail = first.status, f"split failed: {first.detail}"
            elif isinstance(first, DriveModelCallFailed):
                status = _failed_status(first)
            else:
                logger.error(f"[DriveReader] window failed unexpectedly: {type(first).__name__}: {first}",
                             exc_info=first)
            return ExtractionOutcome(status=status, pages_total=pages_total, pages_sent=run.total("pages_sent"),
                                     model_calls=run.total("calls"), model_cost=run.total("cost"),
                                     model_fallback_cost=run.total("fallback_cost"), detail=detail)
        finished = [w for w in run.windows if w.finished]
        cut = len(run.windows) - len(finished)
        if cut:
            logger.warning(f"[DriveReader] file budget {self._file_budget_s:.0f} s reached: {cut} of "
                           f"{len(run.windows)} windows not read")
        text = "\n\n".join(w.text for w in finished if w.text)
        refused = run.total("refused")
        if refused and refused >= covered:
            # every page sent was refused even alone: the same bytes get the same answer, so the file is terminal
            # (R3-P1) — too_large when any page was refused as too large, else unsupported
            status = S.TOO_LARGE if run.total("refused_too_large") else S.UNSUPPORTED
            return ExtractionOutcome(status=status, pages_total=pages_total, pages_sent=run.total("pages_sent"),
                                     model_calls=run.total("calls"), model_cost=run.total("cost"),
                                     model_fallback_cost=run.total("fallback_cost"),
                                     pages_refused=refused, detail=f"every page refused ({refused})")
        return self._outcome(
            text, run.windows[0].brief, pages_total=pages_total, pages_sent=run.total("pages_sent"),
            truncated=pages_total > covered or bool(cut) or any(w.truncated for w in run.windows),
            calls=run.total("calls"), cost=run.total("cost"), fallback_cost=run.total("fallback_cost"),
            timed_out=bool(cut) and not text, pages_refused=refused,
            capture=_dominant([_dominant(w.captures) for w in finished]),
            detail=f"windows={len(run.windows)} halves={run.total('halves')} cut={cut} refused={refused}")

    async def _read_pdf_limited(self, agent: DriveDocumentReaderAgent, request: ExtractionRequest, run: _FileRun,
                                pages_total: int, covered: int) -> ExtractionOutcome:
        """The PDF read of an interactive open (`deadline_at` and/or `text_budget` set, EXTERNAL_FILES_RFC §4.4).
        Window 1 alone, then one window at a time in page order (so the text read so far is one plain sum, no counter
        shared across tasks); nothing is cut or called once the text reaches `text_budget` or the deadline passed.
        At the deadline the window in flight is cancelled and the finished windows are kept. The joined text is
        trimmed to the budget at a paragraph. An outage mid-file returns the finished prefix; with none, it raises."""
        end = time.time() + self._file_budget_s
        if request.deadline_at is not None:
            end = min(end, request.deadline_at)
        budget = request.text_budget
        stop = ""          # "", "budget", "deadline", "unavailable"
        unavailable: Optional[DriveModelUnavailable] = None
        parser_down: Optional[ParserUnavailable] = None
        failure: Optional[BaseException] = None
        with tempfile.TemporaryDirectory(prefix="drive-reader-", dir=self._temp_root) as work:
            for i, win in enumerate(run.windows):
                if budget is not None and _joined_len(run.windows) >= budget:
                    stop = "budget"
                    break
                remaining = end - time.time()
                if remaining <= 0:
                    stop = "deadline"
                    break
                task = asyncio.create_task(self._window(agent, request, i, win, asyncio.Semaphore(1), work))
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if not done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    stop = "deadline"
                    break
                exc = None if task.cancelled() else task.exception()
                if exc is None:
                    continue
                if isinstance(exc, DriveModelUnavailable):
                    stop, unavailable = "unavailable", exc
                elif isinstance(exc, ParserUnavailable):
                    parser_down = exc
                elif isinstance(exc, ParserBusy) or (isinstance(exc, PdfSplitFailed) and exc.detail == "deadline"):
                    stop = "deadline"
                else:
                    failure = exc
                break
        spend = dict(pages_sent=run.total("pages_sent"), model_calls=run.total("calls"),
                     model_cost=run.total("cost"), model_fallback_cost=run.total("fallback_cost"))
        if parser_down is not None:
            raise ParserUnavailable(parser_down.reason, pages_sent=spend["pages_sent"],
                                    model_calls=spend["model_calls"], model_cost=spend["model_cost"],
                                    model_fallback_cost=spend["model_fallback_cost"]) from parser_down
        finished = [w for w in run.windows if w.finished]
        if stop == "unavailable" and not finished:
            if unavailable is None:
                raise RuntimeError("reader stopped as unavailable without the model error")
            raise DriveModelUnavailable(str(unavailable), pages_sent=spend["pages_sent"],
                                        model_calls=spend["model_calls"], model_cost=spend["model_cost"],
                                        model_fallback_cost=spend["model_fallback_cost"],
                                        charges_file=unavailable.charges_file) from unavailable
        if failure is not None:
            status, detail = S.ERROR, str(failure)
            if isinstance(failure, PdfSplitFailed):
                status, detail = failure.status, f"split failed: {failure.detail}"
            elif isinstance(failure, DriveModelCallFailed):
                status = _failed_status(failure)
            else:
                logger.error(f"[DriveReader] window failed unexpectedly: {type(failure).__name__}: {failure}",
                             exc_info=failure)
            return ExtractionOutcome(status=status, pages_total=pages_total, detail=detail, **spend)
        unread = len(run.windows) - len(finished)
        if stop == "deadline":
            logger.warning(f"[DriveReader] item={request.item_id}: the deadline reached, {unread} of "
                           f"{len(run.windows)} windows not read")
        elif stop == "unavailable":
            logger.error(f"[DriveReader] item={request.item_id}: the model became unavailable, {len(finished)} of "
                         f"{len(run.windows)} windows kept")
        refused = run.total("refused")
        if refused and refused >= covered:
            status = S.TOO_LARGE if run.total("refused_too_large") else S.UNSUPPORTED
            return ExtractionOutcome(status=status, pages_total=pages_total, pages_refused=refused,
                                     detail=f"every page refused ({refused})", **spend)
        kept, trimmed_in, trimmed = finished, None, False
        text = "\n\n".join(w.text for w in finished if w.text)
        if budget is not None and len(text) > budget:
            text, trimmed = head_at_paragraph(text, budget), True
            kept, trimmed_in = _split_at(finished, len(text))
        pages_read = sum(w.pages - len(w.skipped) for w in kept)
        detail = (f"windows={len(run.windows)} read={len(finished)} stop={stop or 'end'} refused={refused}"
                  if stop != "unavailable" else "model unavailable")
        outcome = self._outcome(
            text, run.windows[0].brief, pages_total=pages_total, pages_sent=spend["pages_sent"],
            truncated=(pages_total > covered or bool(unread) or trimmed
                       or any(w.truncated for w in run.windows)),
            calls=spend["model_calls"], cost=spend["model_cost"], fallback_cost=spend["model_fallback_cost"],
            timed_out=stop == "deadline" and not text, pages_refused=refused,
            capture=_dominant([_dominant(w.captures) for w in kept]), detail=detail)
        skipped = tuple(p for w in run.windows if w in kept for p in w.skipped)
        cut_within = (tuple(range(trimmed_in.first_page + 1, trimmed_in.first_page + trimmed_in.pages + 1))
                      if trimmed_in is not None else ())
        return replace(outcome, pages_read=pages_read if outcome.status is S.TEXT else 0,
                       skipped_pages=skipped, cut_within_pages=cut_within)

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
        - HTTP 413, 400 or 422: halved, down to one page; a single page still refused is skipped (`truncated`, counted
          in `refused`), and the rest of the file is read (R3-P1).
        `brief`: this piece starts the document, so it asks for the brief (the first piece that answers sets it)."""
        try:
            reply = await self._call(agent, request, work, index, win, first_page, pages, first=brief)
        except (DriveInputTooLarge, DriveInputRefused) as exc:
            if pages <= 1:
                logger.warning(f"[DriveReader] window {index + 1} page {first_page + 1} refused ({exc}); skipped")
                win.refused += 1
                win.skipped.append(first_page + 1)
                win.refused_too_large += isinstance(exc, DriveInputTooLarge)
                win.truncated = True
                return []
            logger.warning(f"[DriveReader] window {index + 1} pages {first_page + 1}+{pages} refused ({exc}); halved")
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
            if reply.capture:
                win.captures.append(reply.capture)
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
        # the right half asks for the brief when the left one did and got none (a skipped page, the output limit)
        texts += await self._piece(agent, request, work, index, win, first_page + left, pages - left,
                                   brief=brief and win.brief is None, may_halve=may_halve)
        return texts

    async def _cut(self, request: ExtractionRequest, work: str, first_page: int, pages: int) -> bytes:
        """One window, cut on demand from the source and removed once read: the call holds it in memory only."""
        out = os.path.join(work, f"pages-{first_page + 1:04d}-{pages:03d}.pdf")
        try:
            if request.deadline_at is None:
                await self._splitter.cut_pdf_window(request.path, first_page, pages, out)
            else:
                await self._splitter.cut_pdf_window(request.path, first_page, pages, out,
                                                    deadline_at=request.deadline_at)
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
            win.fallback_cost += getattr(exc, "model_fallback_cost", 0.0)
            raise
        win.calls += reply.calls
        win.cost += reply.cost
        win.fallback_cost += reply.fallback_cost
        win.pages_sent += pages
        return reply

    @staticmethod
    def _outcome(text: str, brief: Optional[DocumentBrief], *, pages_total: int, pages_sent: int, truncated: bool,
                 calls: int, cost: float, timed_out: bool, detail: str, pages_refused: int = 0,
                 fallback_cost: float = 0.0, capture: str = "") -> ExtractionOutcome:
        if text.strip():
            # the model's capture is `automatic`; the indexing service settles it against the parser's (§26.3)
            return ExtractionOutcome(status=S.TEXT, text=text, brief=brief, pages_total=pages_total,
                                     pages_sent=pages_sent, truncated=truncated,
                                     extracted_by=f"{READER}@{READER_VERSION}", detail=detail, model_calls=calls,
                                     model_cost=cost, model_fallback_cost=fallback_cost, pages_refused=pages_refused,
                                     capture=capture, capture_source="automatic" if capture else "")
        # No text while the budget cut any window is not "the file has no text" (deterministic, §24.8) — the cut
        # windows were never read (R2 P-m4) — nor a parser bomb (`timeout`): a per-file transient, retried with
        # backoff (§8.2).
        return ExtractionOutcome(status=S.ERROR if timed_out else S.NO_TEXT, pages_total=pages_total,
                                 pages_sent=pages_sent, truncated=truncated, detail=detail, model_calls=calls,
                                 model_cost=cost, model_fallback_cost=fallback_cost, pages_refused=pages_refused)


def _joined_len(windows: List[_WindowRun]) -> int:
    return len("\n\n".join(w.text for w in windows if w.finished and w.text))


def _split_at(finished: List[_WindowRun], room: int) -> "tuple[List[_WindowRun], Optional[_WindowRun]]":
    """The finished windows whose text lies wholly in the first `room` characters of the joined text, and the one
    window the cut falls inside (None when it falls between windows)."""
    kept: List[_WindowRun] = []
    offset = 0
    for w in finished:
        if not w.text:
            kept.append(w)
            continue
        start = offset + (2 if offset else 0)
        end = start + len(w.text)
        if end <= room:
            kept.append(w)
            offset = end
            continue
        return kept, (w if room > start else None)
    return kept, None


def _dominant(captures: List[str]) -> str:
    """The capture most of `captures` agree on, the first seen winning a tie; "" when none said one. A window's pieces
    (a halved window) are one vote: `_dominant` of them is the window's, and the file's is `_dominant` of those."""
    return Counter(c for c in captures if c).most_common(1)[0][0] if any(captures) else ""


def _failed_status(exc: DriveModelCallFailed) -> ExtractionStatus:
    """A refusal the same bytes always get is terminal (R2 P-m5): retried, it would only bill again. Only an image
    reaches here refused — a refused PDF page is skipped in `_piece` (R3-P1) — so: an image still refused with 413 →
    `too_large`; refused outright (400/422) → `unsupported`. Anything else (invalid output, a safety stop, a half
    that timed out again) is a per-file transient, `error`."""
    if isinstance(exc, DriveInputTooLarge):
        return S.TOO_LARGE
    if isinstance(exc, DriveInputRefused):
        return S.UNSUPPORTED
    return S.ERROR
