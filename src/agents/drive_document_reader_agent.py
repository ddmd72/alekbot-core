"""
DriveDocumentReaderAgent — reads one PDF window or one image for the drive index
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.4, D8, D12).

Public interface:
    AGENT_TYPE = "drive_reader"
    ReaderReply(text, brief, output_limited, cost)
    DriveDocumentReaderAgent(config, execution_context, prompt_builder, thinking=None)
        .read(data=, mime_type=, file_name=, first_window=, account_id=) -> ReaderReply
            raises DriveModelCallFailed (this input) or DriveModelUnavailable (the model, now)

The file goes in as bytes; out comes the verbatim text and, for the first window only, the brief. Structured output
per the standard (src/adapters/CLAUDE.md): OUTPUT_FORMAT_DRIVE_READER token + response_schema +
response_mime_type, json.loads with one parse retry and no regex fallback. A reply stopped at the output limit is
reported (`output_limited`) for the runner to split the window (§24.3); its truncated JSON is never parsed.

Not a delegation agent (no manifest descriptor, can_handle False), like EmailClassificationAgent; built per file by
`DriveReaderRunner` (composition) with the user's configuration. No tools, no history, no biographical context, no
user prompt overrides: the transcription must not vary with a chat persona. A missing prompt fails the call.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from typing import Any, List, Optional, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.drive_index import DocumentBrief
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import DRIVE_READER
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent
from ..infrastructure.drive_model_call import DriveModelCallFailed, build_drive_prompt, classify_model_error

AGENT_TYPE = "drive_reader"

DOC_TYPES = ("invoice", "receipt", "contract", "statement", "letter", "ticket", "certificate", "form", "other")
_MAX_PARTIES = 5
# Bounds on model output that lands in the card (§24.4), not repairs: longer values are cut, never rewritten.
_MAX_DESCRIPTION_CHARS = 300
_MAX_PARTY_CHARS = 120
_MAX_AMOUNT_CHARS = 60

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["text", "brief"],
    "properties": {
        "text": {"type": "string",
                 "description": "Every word of the input, verbatim, in reading order; tables as Markdown tables."},
        "brief": {
            "type": ["object", "null"],
            "description": "What the document is. Only when the task asks for it; otherwise null.",
            "required": ["doc_type", "description", "doc_date", "parties", "amount"],
            "properties": {
                "doc_type": {"type": "string", "enum": list(DOC_TYPES),
                             "description": "The kind of document."},
                "description": {"type": "string",
                                "description": "One line on what this document is, in the document's language."},
                "doc_date": {"type": ["string", "null"],
                             "description": "The document's own date as YYYY-MM-DD, or null."},
                "parties": {"type": "array", "items": {"type": "string"},
                            "description": "At most 5 names of people or organisations the document is between."},
                "amount": {
                    "type": ["object", "null"],
                    "description": "The document's main amount, or null.",
                    "required": ["value", "currency"],
                    "properties": {
                        "value": {"type": "string", "description": "The amount as printed."},
                        "currency": {"type": "string", "description": "ISO 4217 code, or the symbol as printed."},
                    },
                },
            },
        },
    },
}

_TASK_FIRST = ("Read this document. Transcribe all of it into `text` and fill `brief` from it, as the output "
               "format says.")
_TASK_LATER = ("Read this part of a longer document. Transcribe all of it into `text` and set `brief` to null, as the "
               "output format says.")
_CORRECTION = ("Your previous output was not valid: {error}. Output only the JSON object required by output_format: "
               "no markdown, no explanation.")


@dataclass(frozen=True)
class ReaderReply:
    text: str
    brief: Optional[DocumentBrief] = None
    output_limited: bool = False      # stopped at the output limit: the runner splits the window (§24.3)
    cost: float = 0.0                 # USD, already billed to the account


class DriveDocumentReaderAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._thinking = thinking or DRIVE_READER.thinking
        self._system: Optional[str] = None
        self._system_lock = asyncio.Lock()
        # Breaker accounting for the provider; the strategy has no fallback, so a failure is never re-served.
        self._set_execution_context(execution_context)

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="DriveDocumentReaderAgent is not part of the delegation chain; use read()")

    async def read(self, *, data: bytes, mime_type: str, file_name: str, first_window: bool,
                   account_id: str) -> ReaderReply:
        """One call, billed to `account_id` as its own execution (§24.9)."""
        with self._execution_billing_scope(account_id) as ledger:
            try:
                reply = await self._read(data, mime_type, file_name, first_window)
            except Exception as exc:
                mapped = classify_model_error(exc, AGENT_TYPE)
                if mapped is exc:
                    raise
                raise mapped from exc
            finally:
                await self._flush_billing()
            return replace(reply, cost=ledger.cost())

    # -- implementation -----------------------------------------------------------------------------------------

    async def _read(self, data: bytes, mime_type: str, file_name: str, first_window: bool) -> ReaderReply:
        system = await self._system_prompt()
        file_part = MessagePart(file_data={"base64": base64.b64encode(data).decode("ascii"), "mime_type": mime_type,
                                           "name": file_name})
        history: List[Message] = [Message(role="user", parts=[
            file_part, MessagePart(text=_TASK_FIRST if first_window else _TASK_LATER)])]
        for attempt in range(DRIVE_READER.max_parse_retries + 1):
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=system,
                messages=history,
                max_tokens=DRIVE_READER.max_tokens,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                thinking=self._thinking,
                allow_provider_storage=False,   # verbatim content of the user's files (§24.2)
                timeout=DRIVE_READER.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[{AGENT_TYPE}] output limit reached on {file_name}")
                return ReaderReply(text="", output_limited=True)
            if response.finish_reason is FinishReason.SAFETY:
                raise DriveModelCallFailed("the provider's safety filter stopped the answer")
            raw = response.text or ""
            try:
                return _parse(raw, first_window)
            except ValueError as err:
                logger.warning(f"[{AGENT_TYPE}] invalid output on {file_name} (attempt {attempt + 1}): {err}")
                if attempt >= DRIVE_READER.max_parse_retries:
                    raise DriveModelCallFailed(f"invalid output after the parse retry: {err}") from err
                history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                history.append(Message(role="user", parts=[MessagePart(text=_CORRECTION.format(error=err))]))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _system_prompt(self) -> str:
        async with self._system_lock:
            if self._system is None:
                self._system = await build_drive_prompt(self._prompt_builder, AGENT_TYPE)
            return self._system


def _parse(raw: str, first_window: bool) -> ReaderReply:
    data = json.loads(raw)  # JSONDecodeError is a ValueError: the caller's retry path
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ValueError("`text` must be a string inside a JSON object")
    brief_raw = data.get("brief")
    brief = _brief(brief_raw) if first_window and brief_raw is not None else None
    return ReaderReply(text=data["text"].strip(), brief=brief)


def _brief(raw: Any) -> DocumentBrief:
    if not isinstance(raw, dict):
        raise ValueError("`brief` must be an object or null")
    doc_type = str(raw.get("doc_type") or "").strip().lower()
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    description = " ".join(str(raw.get("description") or "").split())[:_MAX_DESCRIPTION_CHARS]
    parties_raw = raw.get("parties") or []
    if not isinstance(parties_raw, list):
        raise ValueError("`brief.parties` must be an array")
    parties: Tuple[str, ...] = tuple(
        p for p in (" ".join(str(x).split())[:_MAX_PARTY_CHARS] for x in parties_raw) if p)[:_MAX_PARTIES]
    return DocumentBrief(doc_type=doc_type, description=description, doc_date=_doc_date(raw.get("doc_date")),
                         parties=parties, amount=_amount(raw.get("amount")))


def _doc_date(value: Any) -> Optional[datetime]:
    """YYYY-MM-DD as midnight UTC; anything else is no date (the brief is interpretation; a bad date must not cost
    the transcription)."""
    if value in (None, ""):
        return None
    try:
        return datetime.combine(date.fromisoformat(str(value).strip()), time(0), tzinfo=timezone.utc)
    except ValueError:
        logger.warning(f"[{AGENT_TYPE}] brief date not ISO: {str(value)[:20]!r}; dropped")
        return None


def _amount(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise ValueError("`brief.amount` must be an object or null")
    shown = f"{str(value.get('value') or '').strip()} {str(value.get('currency') or '').strip()}".strip()
    return " ".join(shown.split())[:_MAX_AMOUNT_CHARS]
