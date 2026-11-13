"""
EmailCardAgent — writes the card answer of one mail (email index v2 RFC §3, §7 "Card writer"; slice 3).

Public interface:
    AGENT_TYPE = "email_card"
    EmailCardAgent(config, execution_context, prompt_builder)
        .write(account_id, static_block, card_input_text) -> CardWriterReply
            never raises: ok / invalid / refused / unavailable (`EmailCardWriterPort` says which side each is on)

In: the mail block the card core built (`CardInput.text`) followed by the task line; the system part is the two Firestore
tokens plus the account's static block (the owner and the confirmed people, per-render handles), assembled once per
distinct block and cached (at most 16). Out: a `MailAnswer` parsed by the core (`parse_mail_answer`, `json.loads`, one
parse retry, no regex fallback). The model is pinned Gemini Flash-Lite with no fallback: an outage leaves the card
pending. No tools, history, biography or user overrides, and no content capture (mail bodies are never stored: the
runner passes no prompt-content store). A prompt missing a section, or a token placeholder code cannot fill, is an
outage, never a fallback prompt. Not a delegation agent (can_handle False); built once by `EmailCardRunner`.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Awaitable, Callable, List, Optional, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.email_card import MailAnswer
from ..domain.email_card_answer import parse_mail_answer
from ..domain.email_card_prompt import card_response_schema, card_task_line, render_card_prompt
from ..domain.email_card_reply import CardWriterReply, CardWriterStatus
from ..domain.exceptions import (
    LLMAccountError, LLMClientError, LLMError, LLMFallbackClientError, LLMRefusalError,
)
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import EMAIL_CARD
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "email_card"

# The assembled prompt must carry both sections of the blueprint; a token missing from Firestore assembles silently
# without its section (a WARNING in the assembly service), so the agent checks.
_REQUIRED_SECTIONS = ("cognitive_process {", "output_format {")
# A client error with one of these statuses is about this mail's content, not the account.
_REFUSED_STATUSES = frozenset({400, 413, 422})
_CORRECTION = ("Your previous output was not valid: {error}. Output only the JSON object the output format "
               "describes.")
_SYSTEMS_MAX = 16


class _PromptUnavailable(Exception):
    """The Firestore prompt could not be assembled whole: an outage."""


class _SystemCache:
    """The assembled system prompts by block digest, at most `_SYSTEMS_MAX` (the oldest dropped). Owns its lock, so the
    agent instance shared by concurrent requests holds no mutable state of its own on the request path."""

    def __init__(self) -> None:
        self._items: "OrderedDict[str, str]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_assemble(self, key: str, assemble: Callable[[], Awaitable[str]]) -> str:
        async with self._lock:
            system = self._items.get(key)
            if system is None:
                system = await assemble()
                self._items[key] = system
                while len(self._items) > _SYSTEMS_MAX:
                    self._items.popitem(last=False)
            return system


class EmailCardAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._systems = _SystemCache()
        self._set_execution_context(execution_context)   # breaker accounting

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="EmailCardAgent is not part of the delegation chain; use write()")

    async def write(self, account_id: str, static_block: str, card_input_text: str) -> CardWriterReply:
        """One call (plus at most one parse retry), billed to `account_id` as one execution. Never raises."""
        calls = [0]
        with self._execution_billing_scope(account_id) as ledger:
            answer: Optional[MailAnswer] = None
            try:
                status, answer = await self._write(static_block, card_input_text, calls)
            except _PromptUnavailable:
                status = CardWriterStatus.UNAVAILABLE
            except Exception as exc:
                status = _classify(exc)
            finally:
                await self._flush_billing()
            return CardWriterReply(status=status, answer=answer, model=ledger.dominant_model or self._model_name,
                                   usd=ledger.cost(), calls=calls[0])

    # -- implementation -----------------------------------------------------------------------------------------

    async def _write(self, static_block: str, card_input_text: str, calls: List[int]) -> Tuple[CardWriterStatus, Optional[MailAnswer]]:
        system = await self._system_prompt(static_block)
        history: List[Message] = [Message(role="user", parts=[MessagePart(
            text=f"{card_input_text}\n\n{card_task_line()}")])]
        schema = card_response_schema()
        for attempt in range(EMAIL_CARD.max_parse_retries + 1):
            calls[0] += 1
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=system,
                messages=list(history),
                max_tokens=EMAIL_CARD.max_tokens,
                response_mime_type="application/json",
                response_schema=schema,
                thinking=EMAIL_CARD.thinking,
                allow_provider_storage=False,   # the mail's text
                timeout=EMAIL_CARD.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[EmailCard] output limit reached (attempt {attempt + 1})")
                return CardWriterStatus.INVALID, None
            if response.finish_reason is FinishReason.SAFETY:
                logger.warning("[EmailCard] the provider's safety filter stopped the answer")
                return CardWriterStatus.REFUSED, None
            raw = response.text or ""
            try:
                return CardWriterStatus.OK, parse_mail_answer(raw)
            except ValueError as err:
                # The error text only: it names a field or a JSON position, never mail content.
                logger.warning(f"[EmailCard] invalid output (attempt {attempt + 1}): {err}")
                if attempt >= EMAIL_CARD.max_parse_retries:
                    return CardWriterStatus.INVALID, None
                history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                history.append(Message(role="user", parts=[MessagePart(text=_CORRECTION.format(error=err))]))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _system_prompt(self, static_block: str) -> str:
        """The Firestore prompt with the account's block, assembled once per distinct block (at most 16 kept, the
        oldest dropped). A prompt missing a section, or one code cannot render, is evicted from the assembly cache and
        is an outage; nothing is kept."""
        key = hashlib.sha256(static_block.encode("utf-8")).hexdigest()
        return await self._systems.get_or_assemble(key, lambda: self._assemble(static_block))

    async def _assemble(self, static_block: str) -> str:
        try:
            prompt = await self._prompt_builder.build_for_agent(
                agent_type=AGENT_TYPE, user_id=None, account_id=None, include_biographical=False,
                include_directives=False, include_location=False, kb_preamble=False,
                extra_static_blocks=[static_block])
        except Exception as exc:
            logger.error(f"[EmailCard] prompt assembly failed: {type(exc).__name__}: {exc}")
            raise _PromptUnavailable() from exc
        missing = [s for s in _REQUIRED_SECTIONS if s not in (prompt or "")]
        if missing:
            for section in missing:
                logger.error(f"[EmailCard] token missing: {section.rstrip(' {')}")
        else:
            try:
                return render_card_prompt(prompt)
            except ValueError as exc:
                logger.error(f"[EmailCard] token cannot be rendered: {exc}")
        # The assembly service cached this template under this agent's key: drop that one entry, so the prompt is
        # assembled afresh once the tokens are uploaded.
        self._prompt_builder.invalidate_agent_prompt(AGENT_TYPE)
        raise _PromptUnavailable()


def _classify(exc: BaseException) -> CardWriterStatus:
    """Refused (this mail's content) or unavailable (an outage), logging it."""
    if isinstance(exc, LLMAccountError):
        logger.error(f"[EmailCard] provider rejected the account (http={exc.http_status}): {str(exc)[:300]}")
        return CardWriterStatus.UNAVAILABLE
    if isinstance(exc, LLMFallbackClientError):
        logger.warning(f"[EmailCard] fallback refused the input (http={exc.http_status}): {str(exc)[:300]}")
        return CardWriterStatus.UNAVAILABLE
    if isinstance(exc, LLMRefusalError):
        logger.warning(f"[EmailCard] the model declined the mail: {str(exc)[:300]}")
        return CardWriterStatus.REFUSED
    if isinstance(exc, LLMClientError) and exc.http_status in _REFUSED_STATUSES:
        logger.warning(f"[EmailCard] provider refused the mail (http={exc.http_status}): {str(exc)[:300]}")
        return CardWriterStatus.REFUSED
    if isinstance(exc, LLMClientError):
        logger.error(f"[EmailCard] provider rejected the account or model (http={exc.http_status}): {str(exc)[:300]}")
    elif isinstance(exc, (LLMError, asyncio.TimeoutError)):
        logger.warning(f"[EmailCard] model unavailable ({type(exc).__name__}): {str(exc)[:300]}")
    else:
        logger.error(f"[EmailCard] unexpected failure ({type(exc).__name__}): {exc}", exc_info=exc)
    return CardWriterStatus.UNAVAILABLE
