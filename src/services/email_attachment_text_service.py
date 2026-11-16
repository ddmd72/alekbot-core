"""
EmailAttachmentTextService — the attachment text of one mail for the email index v2 (RFC §3 "Attachments").

Given the parts `EmailProviderPort.list_parts` just listed, it drops the noise before any download, downloads only the
candidates, reads the document ones as text through `DocumentReadPort` (never in process: parsing happens in the
sandbox light service) and returns one `EmailAttachment` per LISTED file. One public class.

Load rules: at most `MAX_READ_ATTACHMENTS` reads per mail (a read is a part handed to the document service or served
from the caller's cache), `PARSE_CONCURRENCY` parser calls in flight per service instance, and a back-off of
2 / 5 / 10 s on a busy parser, after which the mail stays pending (`AttachmentParserUnavailable`). The sha256 cache is
owned by the caller (its scope is one job page or run); only outcomes the document service produced are cached, never
an outage.

Scans (slice 5): a PDF the first, parser-only pass answers `scan_not_read` is read a second time by the paid reader
when a `ReaderPageBudget` is given, the scan has `SCAN_MAX_PAGES` pages or fewer (the first pass's count; all or nothing),
the mail's deadline leaves `SCAN_READ_MIN_REMAINING_S` and the budget books its pages. The parser runs twice per read
scan: the page count exists only after it. Any refusal keeps `scan_not_read`; a refusal that depends on the run
(budget, time, a reader outage) is not cached. Logs carry counts only, never a file name or document text.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import Counter
from dataclasses import replace
from typing import Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Set, Tuple

from ..domain.document_limits import parse_input_cap
from ..domain.document_read import CALLER_INDEX, ReadOutcome, ReadRequest, ReadStatus, reader_skipped
from ..domain.email import EmailPart
from ..domain.email_attachment import (
    SKIP_DUPLICATE, SKIP_EMPTY, SKIP_IMAGE, SKIP_NO_TEXT, SKIP_OVER_CAP, SKIP_TIMEOUT, SKIP_TOO_LARGE,
    SKIP_UNREADABLE, SKIP_UNSUPPORTED, AttachmentRead, EmailAttachment,
)
from ..domain.email_attachment_policy import (
    BUSY_RETRY_DELAYS_S, MAIL_READ_KINDS, MAX_READ_ATTACHMENTS, PARSE_CONCURRENCY, SCAN_READ_MIN_REMAINING_S,
    declared_kind, declared_mime, input_slices, noise_reason, scan_admissible, skip_before_download, sniffed_kind,
)
from ..domain.exceptions import AttachmentParserUnavailable, EmailPartTooLarge
from ..domain.file_ref import FileRefKind
from ..domain.oauth_credential import OAuthCredential
from ..ports.document_read_port import DocumentReadPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.reader_page_budget import ReaderPageBudget
from ..utils.logger import logger


class EmailAttachmentTextService:

    def __init__(self, provider: EmailProviderPort, documents: DocumentReadPort, *,
                 concurrency: int = PARSE_CONCURRENCY,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self._provider = provider
        self._documents = documents
        self._slots = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._clock = clock
        # (id of the caller's cache, sha256) -> the read in flight: a concurrent mail with the same bytes waits for it
        # instead of paying for a second read. An entry lives only while its read runs.
        self._inflight: Dict[Tuple[int, str], "asyncio.Future[Tuple[EmailAttachment, bool]]"] = {}

    async def read_attachments(self, credentials: OAuthCredential, message_id: str, parts: Sequence[EmailPart], *,
                               account_id: str, deadline_at: float,
                               cache: MutableMapping[str, EmailAttachment],
                               reader_budget: Optional[ReaderPageBudget] = None) -> Tuple[EmailAttachment, ...]:
        """One outcome per LISTED part, in `parts` order (noise parts left out). `parts` is what
        `EmailProviderPort.list_parts(credentials, message_id)` just returned (handles are valid only with that
        listing). Raises AttachmentParserUnavailable (outage or deadline: the mail stays pending); lets
        EmailMessageGone, EmailPartUnavailable and the OAuth errors of `get_part` propagate unchanged. With a
        `reader_budget` a scan is read by the paid reader when it is admitted (see the module docstring)."""
        if self._clock() >= deadline_at:
            raise AttachmentParserUnavailable("deadline")
        result: List[EmailAttachment] = []
        noise: Counter = Counter()
        seen: Set[str] = set()
        reads = 0
        cache_hits = 0
        for part in parts:
            reason = noise_reason(part)
            if reason:
                noise[reason] += 1
                continue
            early = skip_before_download(part)
            if early:
                result.append(self._skipped(part, declared_kind(part), early))
                continue
            if reads >= MAX_READ_ATTACHMENTS:
                result.append(self._skipped(part, declared_kind(part), SKIP_OVER_CAP))
                continue
            try:
                content = await self._provider.get_part(credentials, message_id, part, parse_input_cap("email"))
            except EmailPartTooLarge:
                result.append(self._skipped(part, declared_kind(part), SKIP_TOO_LARGE))
                continue
            if not content:
                result.append(self._skipped(part, declared_kind(part), SKIP_EMPTY))
                continue
            sha = hashlib.sha256(content).hexdigest()
            kind = sniffed_kind(content, part)
            if sha in seen:
                result.append(self._skipped(part, kind, SKIP_DUPLICATE))
                continue
            seen.add(sha)
            if kind == "image":
                result.append(self._skipped(part, kind, SKIP_IMAGE))
                continue
            if kind not in MAIL_READ_KINDS:
                result.append(self._skipped(part, kind, SKIP_UNSUPPORTED))
                continue
            reads += 1
            if sha in cache:
                cache_hits += 1
                result.append(self._copy(cache[sha], part))
                continue
            key = (id(cache), sha)
            shared = await self._shared_result(key)
            if shared is not None:
                cache_hits += 1
                result.append(self._copy(shared, part))
                continue
            flight: "asyncio.Future[Tuple[EmailAttachment, bool]]" = asyncio.get_running_loop().create_future()
            self._inflight[key] = flight
            try:
                attachment, cacheable = await self._read_one(
                    credentials, part, content, kind, account_id, deadline_at, reader_budget)
            except asyncio.CancelledError:
                flight.cancel()
                raise
            except BaseException as exc:
                flight.set_exception(exc)
                flight.exception()                       # mark retrieved: there may be no waiter
                raise
            else:
                flight.set_result((attachment, cacheable))
                if cacheable:
                    cache[sha] = attachment
            finally:
                self._inflight.pop(key, None)
            result.append(attachment)
        self._log(message_id, result, noise, reads, cache_hits)
        return tuple(result)

    async def _read_one(self, credentials: OAuthCredential, part: EmailPart, content: bytes, kind: str,
                        account_id: str, deadline_at: float,
                        reader_budget: Optional[ReaderPageBudget]) -> Tuple[EmailAttachment, bool]:
        outcome = await self._read(credentials, part, content, account_id, deadline_at)
        attachment, cacheable = self._map(part, kind, outcome)
        if attachment.read is AttachmentRead.SCAN_NOT_READ and reader_budget is not None:
            attachment, cacheable = await self._read_scan(
                credentials, part, content, kind, account_id, deadline_at, reader_budget, outcome.pages_total)
        return attachment, cacheable

    async def _shared_result(self, key: Tuple[int, str]) -> Optional[EmailAttachment]:
        """The cacheable result of the read in flight for `key`, awaited; None when none is running or the leader's result
        was run-dependent (a short deadline, a refused budget, an outage, a raised failure): the caller then reads for
        itself, with its own deadline and its own booking. The wait is shielded, so a cancelled waiter cannot cancel the
        leader's future; the waiter's own cancellation propagates."""
        while True:
            waiting = self._inflight.get(key)
            if waiting is None:
                return None
            try:
                shared, cacheable = await asyncio.shield(waiting)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise                                # this waiter was cancelled
                return None                              # the leader was cancelled
            except Exception:
                return None                              # the leader failed for its own reasons
            return shared if cacheable else None

    @staticmethod
    def _copy(attachment: EmailAttachment, part: EmailPart) -> EmailAttachment:
        """A shared result for another mail: its own name and size, and no money spent again."""
        return replace(attachment, name=part.filename, size=part.size, reader_pages=0, reader_usd=0.0)

    # ----- the read, with the busy back-off -----------------------------------------------------------------

    async def _read(self, credentials: OAuthCredential, part: EmailPart, content: bytes, account_id: str,
                    deadline_at: float, reader_pages: int = 0) -> ReadOutcome:
        """The document service's outcome. On the reader pass a busy answer is retried only while nothing was spent: the
        reader can answer busy after billed model calls, and a retry would pay for the pages again."""
        request = ReadRequest(
            content=content, name=part.filename, declared_mime=declared_mime(part), store=FileRefKind.EMAIL.value,
            caller=CALLER_INDEX, native_formats=(), user_id=credentials.user_id, account_id=account_id,
            deadline_at=deadline_at, request_id=uuid.uuid4().hex, reader_pages=reader_pages)
        delays = iter(BUSY_RETRY_DELAYS_S)
        while True:
            async with self._slots:
                outcome = await self._documents.read(request)
            if not (outcome.status is ReadStatus.PARSER_UNAVAILABLE and outcome.detail == "busy"):
                return outcome
            if reader_pages and (outcome.model_calls or outcome.model_cost_usd):
                return outcome
            delay = next(delays, None)
            if delay is None or self._clock() + delay >= deadline_at:
                raise AttachmentParserUnavailable("busy")
            await self._sleep(delay)

    # ----- the scan path (slice 5) --------------------------------------------------------------------------

    async def _read_scan(self, credentials: OAuthCredential, part: EmailPart, content: bytes, kind: str,
                         account_id: str, deadline_at: float, budget: ReaderPageBudget,
                         pages: int) -> Tuple[EmailAttachment, bool]:
        """The second pass for a scan the first pass left `scan_not_read`: (the attachment, whether it may be cached)."""
        not_read = EmailAttachment(name=part.filename, kind=kind, size=part.size, read=AttachmentRead.SCAN_NOT_READ)
        if not scan_admissible(pages):
            return not_read, True                       # the bytes decide
        if deadline_at - self._clock() < SCAN_READ_MIN_REMAINING_S:
            return not_read, False                      # no booking: the mail's time is short, not the file's
        if not await budget.reserve(credentials.user_id, pages):
            return not_read, False
        outcome = await self._read(credentials, part, content, account_id, deadline_at, reader_pages=pages)
        kind = outcome.kind or kind
        fields = {"reader_pages": pages, "reader_usd": outcome.model_cost_usd}
        if outcome.status is ReadStatus.PARSER_UNAVAILABLE and (outcome.model_calls or outcome.model_cost_usd):
            logger.warning("[EmailAttachment] parser unavailable after a paid reader call; the scan stays scan_not_read")
            return replace(not_read, kind=kind, **fields), False
        if outcome.status is ReadStatus.MODEL_UNAVAILABLE:
            logger.warning("[EmailAttachment] reader unavailable for a scan; it stays scan_not_read")
            return replace(not_read, kind=kind, **fields), False
        if outcome.status is ReadStatus.NO_TEXT or (outcome.status is ReadStatus.TEXT and not outcome.text.strip()):
            return replace(self._skipped(part, kind, SKIP_NO_TEXT), **fields), True
        if outcome.status is ReadStatus.TEXT and any(link.startswith("reader@") for link in outcome.links):
            head, tail = input_slices(kind, outcome.text)
            return EmailAttachment(name=part.filename, kind=kind, size=part.size, read=AttachmentRead.READ_BY_READER,
                                   text=outcome.text, head=head, tail=tail, **fields), True
        attachment, cacheable = self._map(part, kind, outcome)
        if outcome.status is ReadStatus.TEXT:
            return attachment, False                    # the low-density fallback: the parser's text, not the reader's
        return attachment, cacheable

    # ----- the outcome map (RFC §3, card §5 step 8) ---------------------------------------------------------

    def _map(self, part: EmailPart, kind: str, outcome: ReadOutcome) -> Tuple[EmailAttachment, bool]:
        """(the attachment, whether the caller's cache may keep it). Raises for an outage or the deadline."""
        status = outcome.status
        kind = outcome.kind or kind
        if status is ReadStatus.TEXT:
            if not outcome.text.strip():
                return self._skipped(part, kind, SKIP_NO_TEXT), True
            head, tail = input_slices(kind, outcome.text)
            return EmailAttachment(name=part.filename, kind=kind, size=part.size, read=AttachmentRead.TEXT,
                                   text=outcome.text, head=head, tail=tail), True
        if status is ReadStatus.NO_TEXT:
            if reader_skipped(outcome):
                return EmailAttachment(name=part.filename, kind=kind, size=part.size,
                                       read=AttachmentRead.SCAN_NOT_READ), True
            return self._skipped(part, kind, SKIP_NO_TEXT), True
        if status is ReadStatus.ENCRYPTED:
            return EmailAttachment(name=part.filename, kind=kind, size=part.size, read=AttachmentRead.LOCKED), True
        if status is ReadStatus.TOO_LARGE:
            return self._skipped(part, kind, SKIP_TOO_LARGE), True
        if status is ReadStatus.UNSUPPORTED:
            return self._skipped(part, kind, SKIP_UNSUPPORTED), True
        if status is ReadStatus.TIMEOUT:
            if outcome.detail == "deadline":
                raise AttachmentParserUnavailable("deadline")
            return self._skipped(part, kind, SKIP_TIMEOUT), True
        if status in (ReadStatus.MEMORY, ReadStatus.ERROR):
            return self._skipped(part, kind, SKIP_UNREADABLE), True
        if status is ReadStatus.PARSER_UNAVAILABLE:
            raise AttachmentParserUnavailable(outcome.detail or "unavailable")
        if status is ReadStatus.MODEL_UNAVAILABLE:
            raise AttachmentParserUnavailable("model_unavailable")
        logger.error(f"[EmailAttachment] impossible read status {status.value} for the index caller")
        return self._skipped(part, kind, SKIP_UNREADABLE), False

    # ----- helpers --------------------------------------------------------------------------------------------

    @staticmethod
    def _skipped(part: EmailPart, kind: str, reason: str) -> EmailAttachment:
        return EmailAttachment(name=part.filename, kind=kind, size=part.size, read=AttachmentRead.SKIPPED,
                               reason=reason)

    @staticmethod
    def _log(message_id: str, result: Sequence[EmailAttachment], noise: Counter, reads: int, cache_hits: int) -> None:
        outcomes: Dict[str, int] = dict(Counter(a.outcome for a in result))
        logger.info(f"[EmailAttachment] message={message_id} listed={len(result)} outcomes={outcomes} "
                    f"noise={dict(noise)} reads={reads} cache_hits={cache_hits} "
                    f"reader_pages={sum(a.reader_pages for a in result)} "
                    f"reader_usd={sum(a.reader_usd for a in result):.4f}")
