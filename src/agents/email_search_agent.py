"""
Email Search Agent
==================

Specialist agent for the user's indexed email archive.
Three intents, all routed through one agent instance:

  search_emails           — v1: semantic search via 7-stream multi-vector RRF (LLM key extraction).
                            v2 (a card search is wired, EMAIL_SEARCH_V2): one slots call reads the request, then
                            EmailCardSearchService searches the cards (email index v2 §5).
                            payload: {"query": "..."}

  get_email_details       — fetch full Gmail body for a known email_id.
                            payload: {"email_id": "..."}
                            No LLM — direct Gmail API call via EmailSearchService.

  open_email_attachment   — open ONE attachment through the document service (scans read, native files attach).
                            payload: {"file_ref": "email:<msg>:<part>"} or {"email_id": "...", "name": "file.pdf"}
                            No LLM — Gmail API + FileConversionService.open_email.

Routing: execute() dispatches on payload["intent"]:
  open_email_attachment →  _handle_open_attachment (an email: file_ref, or email_id)
  get_email_details     →  _handle_get_details    (requires email_id in payload)
  search_emails         →  _handle_search_emails  (requires query in payload)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.document_open import EmailOpening
from ..domain.document_presentation import present_text
from ..domain.document_read import OPEN_MARGIN_S, ReadOutcome, ReadStatus
from ..domain.email import EmailMetadata
from ..domain.delegation_timestamp import strip_delegation_timestamp
from ..domain.email_open_texts import (
    EMAIL_OPEN_NOT_CONFIGURED_NOTE,
    LIVE_NOTE_NOT_CONNECTED,
    LIVE_NOTE_UNAVAILABLE,
)
from ..domain.exceptions import EmailNotConnected
from ..domain.email_ref import EMAIL_FILE_CONTEXT_KEY, parse_email_ref
from ..domain.file_ref import EMAIL_REF_PREFIX
from ..domain.email_query_slots import (
    SLOTS_PROFILE, render_slots_prompt, slots_response_schema, slots_retry_message, slots_user_message,
)
from ..domain.email_search_query import MailQuerySlots, MailSearchRequest, mail_concepts, parse_query_slots
from ..domain.email_search_render import MailSearchResult
from ..domain.email_search_time import INTENT_PAST, INTENT_PERIOD, choose_window
from ..domain.native_format import NATIVE_FORMATS_KEY, from_plain
from ..domain.untrusted_text import attached_reminder
from ..ports.llm_port import (
    AgentExecutionContext,
    LLMRequest,
    Message,
    MessagePart,
)
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..infrastructure.agent_config import EMAIL_SEARCH

# Fresh-header listing: the newest mail outside the index, and the per-key cap (see the decision record
# docs/04_solution_strategy/decisions/email_search_fresh_headers.md).
FRESH_LIMIT = 100
KEY_LIMIT = 20
MAX_EXACT_KEYS = 3
MIN_KEY_LENGTH = 3
_NO_EMAILS_FOUND = "No emails found matching your query."

_SLOTS_ATTEMPTS = 2
_HISTORY_TURNS = 3

if TYPE_CHECKING:
    from ..services.email_card_search_service import EmailCardSearchService
    from ..services.email_search_service import EmailSearchService
    from ..services.file_conversion_service import FileConversionService


class EmailSearchAgent(BaseAgent):
    """
    Specialist agent for searching the user's email archive.

    Uses LLM to extract semantic search parameters from the user request,
    then delegates 7-stream multi-vector RRF search to EmailSearchService.
    """

    MAX_TOKENS = EMAIL_SEARCH.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        email_search_service: EmailSearchService,
        user_id: Optional[str] = None,
        file_conversion: Optional[FileConversionService] = None,
        user_timezone: str = "UTC",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        card_search: Optional[EmailCardSearchService] = None,
    ) -> None:
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._search_service = email_search_service
        self._file_conversion = file_conversion
        self._card_search = card_search
        self.user_id = user_id
        self._clock = clock
        self._tz_name = user_timezone or "UTC"
        try:
            self._tz: tzinfo = ZoneInfo(self._tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(f"📬 EmailSearchAgent: unknown timezone {user_timezone!r}, using UTC")
            self._tz_name = "UTC"
            self._tz = timezone.utc

        logger.info(
            f"📬 EmailSearchAgent initialized "
            f"(model={self.model_name}, user={user_id[:8] if user_id else 'NONE'})"
        )

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        payload = message.payload
        intent = payload.get("intent", "")
        if intent in ("get_email_details", "open_email_attachment"):
            return True  # email_id validation happens in execute()
        return bool(payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """Route to the appropriate handler based on payload["intent"]."""
        payload = message.payload
        user_id = message.context.get("user_id") or self.user_id
        intent = payload.get("intent", "")
        email_id: str = payload.get("email_id", "")

        if intent == "get_email_details":
            if not email_id:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=(
                        "email_id is required for get_email_details. "
                        "From the prior search_emails result, copy the bracketed email id "
                        'and pass it as context={"email_id": "<value>"}.'
                    ),
                )
            return await self._handle_get_details(message, email_id, user_id)

        if intent == "open_email_attachment":
            return await self._handle_open_attachment(message, user_id)

        return await self._handle_search_emails(message, user_id)

    # ------------------------------------------------------------------
    # Intent: get_email_details
    # ------------------------------------------------------------------

    async def _handle_get_details(
        self, message: AgentMessage, email_id: str, user_id: Optional[str]
    ) -> AgentResponse:
        """Fetch full body of a Gmail message. No LLM involved."""
        logger.info(
            f"📬 EmailSearchAgent.get_details: {email_id} "
            f"user={user_id[:8] if user_id else '?'}"
        )
        start_time = time.time()

        try:
            details = await self._search_service.get_details_with_parts(
                email_id=email_id,
                user_id=user_id or "",
            )
        except Exception as exc:
            logger.error(f"📬 EmailSearchAgent: get_details failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"get_email_details failed: {exc}",
            )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent.get_details done in {duration_ms}ms")

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=details.text,
            confidence=1.0,
            metadata={"email_id": email_id, "duration_ms": duration_ms},
            history_context=self._file_context(details.files),
        )

    # ------------------------------------------------------------------
    # Intent: open_email_attachment
    # ------------------------------------------------------------------

    @staticmethod
    def _file_context(files: Any) -> Optional[Dict[str, Any]]:
        """The `email_file_context` history entry: `[{label, ref}]` per attachment (plan U4 section 6)."""
        entries = [{"label": label, "ref": ref} for label, ref in files]
        return {EMAIL_FILE_CONTEXT_KEY: entries} if entries else None

    async def _handle_open_attachment(self, message: AgentMessage, user_id: Optional[str]) -> AgentResponse:
        """Open one attachment through `FileConversionService.open_email`. No LLM involved.

        Inputs: `file_ref` (an `email:` ref), or `email_id` with `name` (`filename` is the fallback for `name`; a
        `file_ref` without the prefix next to an `email_id` is taken as the name). A choice (several parts match) and
        every note are SUCCESS answers: a failure makes the model retry the same call."""
        payload = message.payload
        file_ref: str = (payload.get("file_ref") or "").strip()
        email_id: str = (payload.get("email_id") or "").strip()
        name: str = (payload.get("name") or payload.get("filename") or "").strip()
        message_id, part_id = "", None
        if file_ref.startswith(EMAIL_REF_PREFIX):
            try:
                message_id, part_id = parse_email_ref(file_ref)
            except ValueError:
                return AgentResponse.failure(
                    task_id=message.task_id, agent_id=self.agent_id,
                    error=(f"'{file_ref}' is not a valid email: ref. Pass the ref exactly as its [Email file: …] "
                           'label shows it, or context={"email_id": "<id>", "name": "<attachment name>"}.'))
        else:
            message_id = email_id
            name = name or file_ref
        if not message_id:
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=("open_email_attachment needs a file_ref (an email: ref from an [Email file: …] label) or an "
                       'email_id: context={"email_id": "<id from search_emails>", "name": "<attachment name>"}.'))
        if self._file_conversion is None:
            return AgentResponse.success(
                task_id=message.task_id, agent_id=self.agent_id, result=EMAIL_OPEN_NOT_CONFIGURED_NOTE,
                confidence=1.0)

        context = message.context
        account_id = context.get("account_id") or ""
        timeout_ms = self.config.timeout_ms
        budget_s = timeout_ms / 1000 - OPEN_MARGIN_S if timeout_ms else 270.0
        logger.info(f"📬 EmailSearchAgent.open_attachment: {message_id}/{part_id or name or '-'} "
                    f"user={user_id[:8] if user_id else '?'}")
        start_time = time.time()
        try:
            opening: EmailOpening = await self._file_conversion.open_email(
                user_id or "", account_id, message_id=message_id, part_id=part_id, name=name or None,
                consumer=from_plain(context.get(NATIVE_FORMATS_KEY)), deadline_at=time.time() + budget_s)
        except Exception as exc:
            logger.error(f"📬 EmailSearchAgent: open_attachment failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=f"open_email_attachment failed: {exc}")
        duration_ms = int((time.time() - start_time) * 1000)
        metadata: Dict[str, Any] = {"email_id": message_id, "duration_ms": duration_ms}

        if opening.needs_choice:
            labels = "\n".join(label for label, _ in opening.candidates)
            return AgentResponse.success(
                task_id=message.task_id, agent_id=self.agent_id, result=f"{opening.note}\n{labels}",
                confidence=1.0, metadata=metadata, history_context=self._file_context(opening.candidates))
        opened = opening.opened
        if opened is None or opened.note:
            return AgentResponse.success(
                task_id=message.task_id, agent_id=self.agent_id, result=opening.note or (opened.note if opened else ""),
                confidence=1.0, metadata=metadata)

        files = ((opening.label, opening.ref),)
        if opened.native_path:
            metadata["file_data"] = {"path": opened.native_path, "mime_type": opened.native_mime}
            result = (f"{attached_reminder('email')}\n{opening.label}\n"
                      f"File '{opening.name}' ({opened.native_mime}) is attached. You can see and analyse it directly.")
        else:
            outcome = opened.outcome or ReadOutcome(status=ReadStatus.TEXT)
            text = present_text(opened.body, store="email", ref=opening.ref, label=opening.name,
                                nonce=secrets.token_hex(8), outcome=outcome)
            result = f"{text}\n{opening.label}"
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=result, confidence=1.0, metadata=metadata,
            history_context=self._file_context(files))

    # ------------------------------------------------------------------
    # Intent: search_emails
    # ------------------------------------------------------------------

    async def _handle_search_emails(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        """v2 when a card search is wired, else LLM key extraction + 7-stream multi-vector RRF search."""
        if self._card_search is not None:
            return await self._handle_search_emails_v2(message, user_id, self._card_search)
        query = message.payload.get("query", "")
        account_id = message.context.get("account_id")
        history: List[dict] = message.payload.get("history", [])

        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided",
            )

        logger.info(
            f"📬 EmailSearchAgent.search_emails: '{query[:60]}' "
            f"user={user_id[:8] if user_id else '?'} history_turns={len(history)}"
        )
        start_time = time.time()

        # Step 1: LLM extracts search parameters
        keys = await self._extract_search_queries(
            query=query,
            history=history,
            user_id=user_id,
            account_id=account_id,
        )
        primary_query = keys.get("primary_query") or query
        alternative_query = keys.get("alternative_query") or query
        tags: List[str] = keys.get("tags") or []
        date_from = self._parse_date(keys.get("date_from"))
        date_to = self._parse_date(keys.get("date_to"), end_of_day=True)

        logger.info(
            f"📬 EmailSearchAgent: primary='{primary_query[:50]}' "
            f"alt='{alternative_query[:50]}' tags={tags} "
            f"date_from={date_from} date_to={date_to}"
        )

        # Step 2: the index (7-stream RRF), the live headers newer than the index, and the exact keys, together
        uid = user_id or ""
        watermark = await self._safe_watermark(uid)
        fresh_after, run_fresh = self._fresh_window(watermark, date_from, date_to)
        fresh_before = self._fresh_before(date_to)
        exact_keys = self._exact_keys(keys.get("exact_keys"))

        vector_task = self._search_service.vector_search(
            primary_query=primary_query,
            alternative_query=alternative_query,
            tags=tags,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
        )
        fresh_task = (
            self._search_service.list_headers(uid, after=fresh_after, before=fresh_before, limit=FRESH_LIMIT)
            if run_fresh else self._none()
        )
        key_tasks = [self._search_service.list_headers(uid, key=k, limit=KEY_LIMIT) for k in exact_keys]
        vector_res, fresh_res, *key_res = await asyncio.gather(
            vector_task, fresh_task, *key_tasks, return_exceptions=True
        )

        if isinstance(vector_res, BaseException):
            logger.error(f"📬 EmailSearchAgent: vector_search failed: {vector_res}", exc_info=vector_res)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Email search failed: {vector_res}",
            )

        result = self._assemble_result(
            vector_res, fresh_res if run_fresh else None, run_fresh, fresh_after, fresh_before, exact_keys, key_res
        )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent done in {duration_ms}ms")

        history_context_value = self._build_email_history_context(result=result, query=query)

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={
                "duration_ms": duration_ms,
                "primary_query": primary_query,
                "alternative_query": alternative_query,
                "tags": tags,
            },
            history_context={"email_search_context": history_context_value} if history_context_value else None,
        )

    # ------------------------------------------------------------------
    # Intent: search_emails, v2 (email index v2 §5; slice 8)
    # ------------------------------------------------------------------

    async def _handle_search_emails_v2(
        self, message: AgentMessage, user_id: Optional[str], card_search: EmailCardSearchService
    ) -> AgentResponse:
        """The slots call, then the card search, the live headers newer than the index and the per-key lists
        together. The delegation timestamp is stripped first: left in, it would read as a one-day period and as words
        no mail holds. A card-search error fails the search (the switch is the only fallback). The request text is
        never logged."""
        request = strip_delegation_timestamp(message.payload.get("query", "") or "").strip()
        if not request:
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error="No query provided")
        account_id = message.context.get("account_id")
        history: List[dict] = message.payload.get("history") or []
        uid = user_id or ""
        start_time = time.time()
        local_now = self._clock().astimezone(self._tz)
        today = local_now.date()

        slots = await self._read_slots(request, history, user_id, account_id, local_now)
        date_from, date_to = self._v2_bounds(request, slots, today)
        watermark = await self._safe_watermark(uid)
        fresh_after, run_fresh = self._fresh_window(watermark, date_from, date_to)
        fresh_before = self._fresh_before(date_to)
        exact_keys = self._exact_keys(list(slots.exact_keys) if slots else [])

        search_task = card_search.search(uid, account_id or "", MailSearchRequest(request, today, slots))
        fresh_task = (
            self._search_service.list_headers(uid, after=fresh_after, before=fresh_before, limit=FRESH_LIMIT)
            if run_fresh else self._none()
        )
        key_tasks = [self._search_service.list_headers(uid, key=k, limit=KEY_LIMIT) for k in exact_keys]
        search_res, fresh_res, *key_res = await asyncio.gather(
            search_task, fresh_task, *key_tasks, return_exceptions=True
        )
        if isinstance(search_res, BaseException):
            logger.error(f"📬 EmailSearchAgent: card search failed: {search_res}", exc_info=search_res)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=f"Email search failed: {search_res}")

        found = cast(MailSearchResult, search_res)
        seen = {*found.shown_ids, *(i for ids in found.folded_ids.values() for i in ids)}
        fresh, by_key, notes = self._live_lists(
            fresh_res if run_fresh else None, run_fresh, fresh_after, fresh_before, exact_keys, key_res, seen)
        payload = {
            "index": {
                "mode": found.mode,
                "sections": [[section.kind, section.text] for section in found.sections],
                "shown_ids": list(found.shown_ids),
                "folded_ids": {k: list(v) for k, v in found.folded_ids.items()},
                "has_attachments": any(card.attachments for card in found.shown),
            },
            "fresh": fresh,
            "by_key": by_key,
            "notes": notes,
        }
        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent v2 done in {duration_ms}ms mode={found.mode} shown={len(found.shown_ids)} "
                    f"slots={'ok' if slots is not None else 'degraded'}")
        received = self._v2_received(found, fresh, by_key)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=json.dumps(payload, ensure_ascii=False),
            confidence=1.0,
            metadata={"duration_ms": duration_ms, "mode": found.mode, "shown": len(found.shown_ids),
                      "slots": "ok" if slots is not None else "degraded"},
            history_context=(
                {"email_search_context": {"you_searched": request, "you_received": received}} if received else None),
        )

    async def _read_slots(
        self, request: str, history: List[dict], user_id: Optional[str], account_id: Optional[str],
        local_now: datetime,
    ) -> Optional[MailQuerySlots]:
        """The validated slots of the request, or None (degraded, logged): no prompt means no model call, an output
        that is not a JSON object is retried once, a failed call is not retried. Never raises."""
        started = time.monotonic()

        def degraded(reason: str) -> None:
            logger.info(f"[EmailQuerySlots] user={(user_id or '?')[:8]} outcome=degraded:{reason} "
                        f"ms={int((time.monotonic() - started) * 1000)}")

        try:
            system = render_slots_prompt(await self.prompt_builder.build_for_agent(
                SLOTS_PROFILE, user_id=user_id, account_id=account_id, include_biographical=False,
                include_directives=False, include_location=False))
        except Exception as exc:
            logger.warning(f"[EmailQuerySlots] prompt build failed: {type(exc).__name__}: {exc}")
            degraded("prompt_build_failed")
            return None

        messages: List[Message] = []
        for turn in history[-_HISTORY_TURNS:]:
            text = turn.get("text", "")
            if text:
                messages.append(Message(role=turn.get("role", "user"), parts=[MessagePart(text=text)]))
        messages.append(Message(role="user", parts=[MessagePart(text=slots_user_message(
            request, local_now, self._tz_name))]))

        for attempt in range(_SLOTS_ATTEMPTS):
            try:
                response = await self._call_llm(LLMRequest(
                    model_name=self.model_name, system_instruction=system, messages=list(messages), tools=[],
                    max_tokens=EMAIL_SEARCH.slots_max_tokens, disable_safety=True,
                    response_mime_type="application/json", response_schema=slots_response_schema(),
                ), turn=attempt)
            except Exception as exc:
                logger.warning(f"[EmailQuerySlots] model call failed: {type(exc).__name__}: {exc}")
                degraded("call_failed")
                return None
            text = (response.text or "").strip()
            try:
                raw = json.loads(text)
            except ValueError as exc:
                reason = f"not JSON: {exc}"
            else:
                if isinstance(raw, dict):
                    slots = parse_query_slots(raw, request)
                    logger.info(
                        f"[EmailQuerySlots] user={(user_id or '?')[:8]} outcome=slots "
                        f"types={','.join(slots.doc_types) or '-'} domains={','.join(slots.domains) or '-'} "
                        f"people={len(slots.people)} time={slots.time.intent if slots.time else '-'} "
                        f"exact={len(slots.exact_keys)} ms={int((time.monotonic() - started) * 1000)}")
                    return slots
                reason = "not a JSON object"
            logger.warning(f"[EmailQuerySlots] invalid output on attempt {attempt + 1}/{_SLOTS_ATTEMPTS}: {reason}")
            messages.append(Message(role="model", parts=[MessagePart(text=text)]))
            messages.append(Message(role="user", parts=[MessagePart(text=slots_retry_message(reason))]))
        degraded("invalid_output")
        return None

    @staticmethod
    def _v2_bounds(request: str, slots: Optional[MailQuerySlots], today: date) -> Tuple[Optional[datetime],
                                                                                       Optional[datetime]]:
        """The live listing's bounds (naive local, as `_parse_date` gives them): the search window's first and last
        day, only when the window filters and looks back (`period`, `past`); else none."""
        window = choose_window(mail_concepts(request), slots, today)
        if (window is None or not window.filters or window.intent not in (INTENT_PERIOD, INTENT_PAST)
                or window.start is None or window.end is None):
            return None, None
        return datetime.combine(window.start, dt_time.min), datetime.combine(window.end, dt_time.max)

    @staticmethod
    def _v2_received(found: MailSearchResult, fresh: Optional[Dict[str, Any]],
                     by_key: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The `you_received` entries (v1 shape): the cards on a line of their own, then the header lines."""
        received: List[Dict[str, Any]] = []
        for card in found.shown:
            entry: Dict[str, Any] = {
                "id": card.mail.email_id,
                "from": card.mail.from_name or card.mail.from_address,
                "date": card.mail.sent_day.isoformat(),
                "summary": (card.profile.title or card.mail.subject)[:200],
            }
            if card.attachments:
                entry["attachments"] = [a.name for a in card.attachments]
            received.append(entry)
        headers = list((fresh or {}).get("emails") or [])
        for group in by_key:
            headers.extend(group.get("emails") or [])
        for h in headers:
            received.append({"id": h.get("email_id", ""), "from": h.get("from", ""), "date": h.get("date", ""),
                             "summary": (h.get("subject") or "")[:200]})
        return received

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _none() -> None:
        return None

    async def _safe_watermark(self, user_id: str) -> Optional[datetime]:
        """The index watermark; a failed read counts as "unknown" (no lower bound), logged."""
        try:
            return await self._search_service.index_watermark(user_id)
        except Exception as exc:
            logger.warning(f"📬 EmailSearchAgent: index watermark unavailable: {exc}", exc_info=True)
            return None

    def _local_to_utc(self, naive: datetime) -> datetime:
        """A wall-clock time of the user's calendar, as an aware UTC instant."""
        return naive.replace(tzinfo=self._tz).astimezone(timezone.utc)

    def _start_of_yesterday(self) -> datetime:
        today: date = self._clock().astimezone(self._tz).date()
        return self._local_to_utc(datetime.combine(today - timedelta(days=1), dt_time.min))

    def _fresh_window(
        self, watermark: Optional[datetime], date_from: Optional[datetime], date_to: Optional[datetime]
    ) -> Tuple[Optional[datetime], bool]:
        """`(after, run)` of the live listing. The window opens at the older of the index watermark and the
        start of yesterday (the user's day); never-indexed has no lower bound. A request bounded to a period the
        index already covers (its end is before the window) skips the listing. A requested start narrows `after`."""
        window_start = min(watermark, self._start_of_yesterday()) if watermark else None
        range_from = self._local_to_utc(date_from) if date_from else None
        range_to = self._local_to_utc(date_to) if date_to else None
        if range_to is not None and window_start is not None and range_to < window_start:
            return None, False
        after = window_start
        if range_from is not None and (after is None or range_from > after):
            after = range_from
        return after, True

    def _fresh_before(self, date_to: Optional[datetime]) -> Optional[datetime]:
        if not date_to:
            return None
        range_to = self._local_to_utc(date_to)
        return range_to if range_to < self._clock() else None

    @staticmethod
    def _exact_keys(raw: object) -> List[str]:
        """The identifiers to match literally: strings only, stripped, at least MIN_KEY_LENGTH, unique, first
        MAX_EXACT_KEYS."""
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            key = item.strip()
            if len(key) >= MIN_KEY_LENGTH and key not in out:
                out.append(key)
        return out[:MAX_EXACT_KEYS]

    def _local_stamp(self, moment: datetime) -> str:
        aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        return aware.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")

    def _header_entry(self, m: EmailMetadata) -> Dict[str, Any]:
        return {
            "email_id": m.email_id,
            "from": m.from_address,
            "subject": m.subject,
            "date": self._local_stamp(m.date),
            "snippet": m.snippet,
        }

    @staticmethod
    def _note_for(exc: BaseException) -> str:
        """The note KIND of a failed live listing (the formatter owns the text)."""
        if isinstance(exc, EmailNotConnected):
            return LIVE_NOTE_NOT_CONNECTED
        logger.warning(f"📬 EmailSearchAgent: live mailbox listing failed: {exc}", exc_info=exc)
        return LIVE_NOTE_UNAVAILABLE

    def _assemble_result(
        self,
        vector_res: str,
        fresh_res: Any,
        run_fresh: bool,
        fresh_after: Optional[datetime],
        fresh_before: Optional[datetime],
        exact_keys: List[str],
        key_res: List[Any],
    ) -> str:
        """One JSON object: the index hits, the fresh headers, the per-key headers and the notes. A listing that
        failed adds a note and no entry; ids already shown earlier are dropped from later lists."""
        index_emails: List[dict] = []
        try:
            parsed = json.loads(vector_res)
            if isinstance(parsed, dict):
                index_emails = parsed.get("emails") or []
        except (ValueError, TypeError):
            index_emails = []
        seen = {e.get("email_id") for e in index_emails}
        fresh, by_key, notes = self._live_lists(fresh_res, run_fresh, fresh_after, fresh_before, exact_keys, key_res,
                                                seen)

        empty = not index_emails and not notes and not (fresh and fresh["emails"]) \
            and not any(g["emails"] for g in by_key)
        if empty:
            return _NO_EMAILS_FOUND
        return json.dumps(
            {"count": len(index_emails), "emails": index_emails, "fresh": fresh, "by_key": by_key, "notes": notes},
            ensure_ascii=False,
        )

    def _live_lists(
        self,
        fresh_res: Any,
        run_fresh: bool,
        fresh_after: Optional[datetime],
        fresh_before: Optional[datetime],
        exact_keys: List[str],
        key_res: List[Any],
        seen: Set[Any],
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """The fresh headers, the per-key headers and the notes. A listing that failed adds a note and no entry; an
        id in `seen` (shown earlier) is dropped, and every id listed here joins it."""
        notes: List[str] = []

        def add_note(exc: BaseException) -> None:
            note = self._note_for(exc)
            if note not in notes:
                notes.append(note)

        fresh: Optional[Dict[str, Any]] = None
        if run_fresh:
            if isinstance(fresh_res, BaseException):
                add_note(fresh_res)
            elif fresh_res is not None:
                metas, more = fresh_res
                entries = []
                for m in metas:
                    if m.email_id not in seen:
                        entries.append(self._header_entry(m))
                        seen.add(m.email_id)
                fresh = {
                    "since": self._local_stamp(fresh_after) if fresh_after else None,
                    "until": self._local_stamp(fresh_before) if fresh_before else None,
                    "listed": len(metas),
                    "tz": self._tz_name,
                    "truncated": bool(more),
                    "emails": entries,
                }

        by_key: List[Dict[str, Any]] = []
        for key, res in zip(exact_keys, key_res):
            if isinstance(res, BaseException):
                add_note(res)
                continue
            metas, more = res
            entries = []
            for m in metas:
                if m.email_id not in seen:
                    entries.append(self._header_entry(m))
                    seen.add(m.email_id)
            by_key.append({"key": key, "truncated": bool(more), "listed": len(metas), "emails": entries})

        return fresh, by_key, notes

    @staticmethod
    def _parse_date(value: object, *, end_of_day: bool = False) -> Optional[datetime]:
        """
        Parse a YYYY-MM-DD string from the LLM output into a datetime. Returns None on any error.

        strptime lands on 00:00:00 — the *start* of that day. date_to is used as an
        inclusive upper bound (email_date <= date_to), so passing it through unchanged
        would exclude every email on the LLM's own end date. end_of_day=True shifts it
        to 23:59:59.999999 so the whole day is included.
        """
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"📬 EmailSearchAgent: could not parse date '{value}', ignoring")
            return None
        if end_of_day:
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
        return parsed

    @staticmethod
    def _build_email_history_context(result: Any, query: str) -> Optional[Dict[str, Any]]:
        """Structured context for history persistence (you_searched / you_received).

        Parses the JSON string returned by EmailSearchService.vector_search.
        Returns None when result is not a parseable JSON email list.
        """
        if not isinstance(result, str):
            return None
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        emails = parsed.get("emails") or []
        fresh = parsed.get("fresh") or {}
        by_key = parsed.get("by_key") or []
        headers = list(fresh.get("emails") or [])
        for group in by_key:
            headers.extend(group.get("emails") or [])
        if not emails and not headers:
            return None
        received = []
        for e in emails:
            entry: Dict[str, Any] = {
                "id": e.get("email_id", ""),
                "from": e.get("from", ""),
                "date": e.get("date", ""),
                "summary": (e.get("text") or "")[:200],
            }
            atts = e.get("attachments") or []
            if atts:
                entry["attachments"] = atts
            received.append(entry)
        for h in headers:
            received.append({
                "id": h.get("email_id", ""),
                "from": h.get("from", ""),
                "date": h.get("date", ""),
                "summary": (h.get("subject") or "")[:200],
            })
        return {"you_searched": query, "you_received": received}

    # ------------------------------------------------------------------
    # Query extraction (used by search_emails only)
    # ------------------------------------------------------------------

    async def _extract_search_queries(
        self,
        query: str,
        history: List[dict],
        user_id: Optional[str],
        account_id: Optional[str],
    ) -> dict:
        """
        Use LLM to extract primary_query, alternative_query, and tags for vector search.
        Includes biographical context and optional conversation history.
        Falls back to empty dict (caller uses raw query) on any error.
        """
        # Build system prompt (separate try so failures don't skip debug logging)
        system_prompt = ""
        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="email_search",
                user_id=user_id,
                account_id=account_id,
                routing_metadata=None,
                include_biographical=True,
            )
        except Exception as exc:
            logger.warning(f"📬 EmailSearchAgent: build_for_agent failed ({exc}), proceeding with empty prompt")

        messages: List[Message] = []

        # Inject last 3 conversation turns as context (if caller provides them)
        for turn in history[-3:]:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            if text:
                messages.append(
                    Message(role=role, parts=[MessagePart(text=text)])
                )

        user_text = f'EMAIL_SEARCH_REQUEST "{query}"'
        messages.append(
            Message(
                role="user",
                parts=[MessagePart(text=user_text)],
            )
        )

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=messages,
            tools=[],
            max_tokens=self.MAX_TOKENS,
            disable_safety=True,
            response_mime_type="application/json",
        )


        try:
            response = await self._call_llm(request)
            raw = (response.text or "").strip()
            logger.debug(f"📬 EmailSearchAgent LLM raw: {raw}")

            return json.loads(raw)

        except Exception as exc:
            logger.warning(
                f"📬 EmailSearchAgent: query extraction failed ({exc}), using raw query"
            )
            return {}
