"""
FactProfilerAgent — profiles one stored fact so it can be found later (docs/10_rfcs/FACT_PROFILE_RFC.md §4.1, §4.3).

Public interface:
    AGENT_TYPE = "fact_profiler"
    response_schema() -> dict          the strict JSON schema of `ProfileAnswer`, enums taken from the domain registry
    FactProfilerAgent(config, execution_context, prompt_builder)
        .profile(account_id, text, metadata) -> FactProfilerReply
            never raises: ok / invalid / refused / unavailable (`FactProfilerPort` says which side each is on)

In: the fact's text and metadata as the one user message `build_profiler_user_message` prints (the bytes the
fingerprint hashes). Out: a `ProfileAnswer` validated against the registry's enums (code derives the domains from it
afterwards). Structured output per the standard (src/adapters/CLAUDE.md): OUTPUT_FORMAT_FACT_PROFILER + the schema
through the provider's native structured output, json.loads, one parse retry, no regex fallback. No tools, history,
biography or user overrides; a prompt missing a section is an outage, never a fallback prompt. Not a delegation agent
(can_handle False); built once by `FactProfilerRunner` (composition), which pins the model.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple, get_args

from pydantic import ValidationError

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.exceptions import (
    LLMAccountError, LLMClientError, LLMError, LLMFallbackClientError, LLMRefusalError,
)
from ..domain.fact_domains import render_subject_enum_block, render_use_area_line, subject_enum, use_area_enum
from ..domain.fact_profile_answer import (
    PROFILE_KINDS, AboutPerson, Precision, ProfileAnswer, Sensitivity, TaxOrFine, TimeRole,
)
from ..domain.fact_profile_fingerprint import build_profiler_user_message
from ..domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import FACT_PROFILER
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "fact_profiler"

# The assembled prompt must carry both sections of the blueprint; a token missing from Firestore assembles silently
# without its section (a WARNING in the assembly service), so the agent checks.
_REQUIRED_SECTIONS = ("cognitive_process {", "output_format {")
# A client error with one of these statuses is about this fact's content, not the account.
_REFUSED_STATUSES = frozenset({400, 413, 422})
_CORRECTION = ("Your previous output was not valid: {error}. Output only the JSON object the output format "
               "describes.")


class _PromptUnavailable(Exception):
    """The Firestore prompt could not be assembled whole: an outage."""


def _strings() -> Dict[str, Any]:
    return {"type": "string"}


def _enum(values: Tuple[str, ...]) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _object(props: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def response_schema() -> Dict[str, Any]:
    """The schema the model answers to: every property required, every object closed, properties in `ProfileAnswer`
    field order, enums from the registry and the answer's own Literals."""
    return _object({
        "label_words": {"type": "array", "items": _strings()},
        "time_role": _enum(get_args(TimeRole)),
        "time_points": {"type": "array", "items": _object({
            "from": _strings(), "to": _strings(), "precision": _enum(get_args(Precision)), "source": _strings()})},
        "places": {"type": "array", "items": _object({
            "city": _strings(), "country": _strings(), "source": _strings()})},
        "people": {"type": "array", "items": _object({
            "name": _strings(), "role": _strings(), "source": _strings()})},
        "tax_or_fine": _enum(get_args(TaxOrFine)),
        "user_identity": {"type": "boolean"},
        "pet": {"type": "boolean"},
        "state_or_law": {"type": "boolean"},
        "about_person": _enum(get_args(AboutPerson)),
        "subject": _enum(subject_enum()),
        "money_flow": {"type": "boolean"},
        "use_areas": {"type": "array", "items": _enum(use_area_enum())},
        "kinds": {"type": "array", "items": _enum(PROFILE_KINDS)},
        "sensitivity": _enum(get_args(Sensitivity)),
    })


class FactProfilerAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._system: Optional[str] = None
        self._system_lock = asyncio.Lock()
        self._set_execution_context(execution_context)   # breaker accounting

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="FactProfilerAgent is not part of the delegation chain; use profile()")

    async def profile(self, account_id: str, text: str, metadata: Optional[Mapping[str, Any]]) -> FactProfilerReply:
        """One call (plus at most one parse retry), billed to `account_id` as one execution. Never raises."""
        calls = [0]
        with self._execution_billing_scope(account_id) as ledger:
            try:
                status, answer = await self._profile(text, metadata, calls)
            except _PromptUnavailable:
                status, answer = ProfilerStatus.UNAVAILABLE, None
            except Exception as exc:
                status, answer = _classify(exc), None
            finally:
                await self._flush_billing()
            return FactProfilerReply(status=status, answer=answer, model=ledger.dominant_model or self._model_name,
                                     usd=ledger.cost(), calls=calls[0])

    # -- implementation -----------------------------------------------------------------------------------------

    async def _profile(self, text: str, metadata: Optional[Mapping[str, Any]],
                       calls: List[int]) -> Tuple[ProfilerStatus, Optional[ProfileAnswer]]:
        system = await self._system_prompt()
        history: List[Message] = [Message(role="user", parts=[MessagePart(
            text=build_profiler_user_message(text, metadata))])]
        schema = response_schema()
        for attempt in range(FACT_PROFILER.max_parse_retries + 1):
            calls[0] += 1
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=system,
                messages=list(history),
                max_tokens=FACT_PROFILER.max_tokens,
                response_mime_type="application/json",
                response_schema=schema,
                thinking=FACT_PROFILER.thinking,
                allow_provider_storage=False,   # the user's fact text
                timeout=FACT_PROFILER.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[FactProfile] output limit reached (attempt {attempt + 1})")
                return ProfilerStatus.INVALID, None
            if response.finish_reason is FinishReason.SAFETY:
                logger.warning("[FactProfile] the provider's safety filter stopped the answer")
                return ProfilerStatus.REFUSED, None
            raw = response.text or ""
            try:
                return ProfilerStatus.OK, _parse(raw)
            except ValueError as err:
                # Field paths and error types only: a value is fact content.
                logger.warning(f"[FactProfile] invalid output (attempt {attempt + 1}): {_describe(err, values=False)}")
                if attempt >= FACT_PROFILER.max_parse_retries:
                    return ProfilerStatus.INVALID, None
                history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                history.append(Message(role="user", parts=[MessagePart(
                    text=_CORRECTION.format(error=_describe(err, values=True)))]))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _system_prompt(self) -> str:
        """The Firestore prompt plus the registry's enum block, assembled once per instance. A prompt missing a
        section is evicted from the builder's cache and is an outage; nothing is cached."""
        async with self._system_lock:
            if self._system is not None:
                return self._system
            enum_block = render_subject_enum_block() + "\n" + render_use_area_line()
            try:
                prompt = await self._prompt_builder.build_for_agent(
                    agent_type=AGENT_TYPE, user_id=None, account_id=None, include_biographical=False,
                    include_directives=False, kb_preamble=False, extra_static_blocks=[enum_block])
            except Exception as exc:
                logger.error(f"[FactProfile] prompt assembly failed: {type(exc).__name__}: {exc}")
                raise _PromptUnavailable() from exc
            missing = [s for s in _REQUIRED_SECTIONS if s not in (prompt or "")]
            if missing:
                for section in missing:
                    logger.error(f"[FactProfile] token missing: {section.rstrip(' {')}")
                # The assembly service cached this incomplete template under this agent's key: drop that one entry,
                # so the prompt is assembled afresh once the tokens are uploaded.
                self._prompt_builder.invalidate_agent_prompt(AGENT_TYPE)
                raise _PromptUnavailable()
            self._system = prompt
            return prompt


def _parse(raw: str) -> ProfileAnswer:
    """The validated answer. A malformed or out-of-enum reply is a ValueError (pydantic's included): the retry."""
    return ProfileAnswer.model_validate(json.loads(raw))


def _describe(err: ValueError, *, values: bool) -> str:
    """What was wrong: for pydantic the field path and the error type (and, for the model's correction turn, the
    message); for a JSON error its own text, which holds no content."""
    if isinstance(err, ValidationError):
        parts = []
        for e in err.errors(include_input=False, include_url=False):
            where = ".".join(str(p) for p in e["loc"]) or "answer"
            parts.append(f"{where}: {e['msg']}" if values else f"{where}: {e['type']}")
        return "; ".join(parts)
    return str(err)


def _classify(exc: BaseException) -> ProfilerStatus:
    """Refused (this fact's content) or unavailable (an outage), logging it. The drive indexer's
    `classify_model_error` is the reference."""
    if isinstance(exc, LLMAccountError):
        # Credit balance or billing: its 400 is not a refusal of this fact.
        logger.error(f"[FactProfile] provider rejected the account (http={exc.http_status}): {str(exc)[:300]}")
        return ProfilerStatus.UNAVAILABLE
    if isinstance(exc, LLMFallbackClientError):
        logger.warning(f"[FactProfile] fallback refused the input (http={exc.http_status}): {str(exc)[:300]}")
        return ProfilerStatus.UNAVAILABLE
    if isinstance(exc, LLMRefusalError):
        logger.warning(f"[FactProfile] the model declined the fact: {str(exc)[:300]}")
        return ProfilerStatus.REFUSED
    if isinstance(exc, LLMClientError) and exc.http_status in _REFUSED_STATUSES:
        logger.warning(f"[FactProfile] provider refused the fact (http={exc.http_status}): {str(exc)[:300]}")
        return ProfilerStatus.REFUSED
    if isinstance(exc, LLMClientError):
        logger.error(f"[FactProfile] provider rejected the account or model (http={exc.http_status}): "
                     f"{str(exc)[:300]}")
    elif isinstance(exc, (LLMError, asyncio.TimeoutError)):
        logger.warning(f"[FactProfile] model unavailable ({type(exc).__name__}): {str(exc)[:300]}")
    else:
        logger.error(f"[FactProfile] unexpected failure ({type(exc).__name__}): {exc}", exc_info=exc)
    return ProfilerStatus.UNAVAILABLE
