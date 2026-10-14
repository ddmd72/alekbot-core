"""
DriveImageTriageAgent — sorts a batch of thumbnails into text to read and photographs to caption
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.4, §24.5, D9, D12).

Public interface:
    AGENT_TYPE = "drive_triage"
    TriageInput(index, data, mime_type, camera_prior)
    TriageReply(verdicts: Mapping[int, (TriageVerdict, caption)], cost, calls)
    DriveImageTriageAgent(config, execution_context, prompt_builder, thinking=None)
        .classify(images, account_id=) -> TriageReply
            raises DriveModelCallFailed (this batch) or DriveModelUnavailable (the model, now), each carrying the
            calls made and their cost (`model_calls`, `model_cost`, from the ledger)

Each thumbnail goes with an opaque index (never the item id) and the camera prior as text. The answer is mapped back
by index: an unknown index is ignored, a repeated one keeps its first answer, and a batch with indexes missing gets
one correction turn; whatever is still missing after it is left out of the reply for the runner to settle. Captions
are photographs only, at most ten words. Structured output per the standard (src/adapters/CLAUDE.md):
OUTPUT_FORMAT_DRIVE_TRIAGE + response_schema + response_mime_type, json.loads, no regex fallback.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.drive_index import TriageVerdict
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import DRIVE_TRIAGE
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent
from ..infrastructure.drive_model_call import (
    CallCounter, DriveModelCallFailed, build_drive_prompt, classify_model_error, with_usage,
)

AGENT_TYPE = "drive_triage"
MAX_CAPTION_WORDS = 10

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "description": "One entry per image, by its index.",
            "items": {
                "type": "object",
                "required": ["index", "verdict", "caption"],
                "properties": {
                    "index": {"type": "integer", "description": "The image's index as given."},
                    "verdict": {"type": "string", "enum": [TriageVerdict.TEXT.value, TriageVerdict.PHOTO.value],
                                "description": "text: the image is there to be read; photo: everything else."},
                    "caption": {"type": "string",
                                "description": "photo: at most ten English words on what it shows; text: empty."},
                },
            },
        },
    },
}

_TASK = "Classify each of the {n} images above by its index, as the output format says."
_CORRECTION = ("Your previous output {problem}. Output only the JSON object required by output_format, with one "
               "entry for every index: {indexes}.")


@dataclass(frozen=True)
class TriageInput:
    index: int
    data: bytes
    mime_type: str
    camera_prior: bool


@dataclass(frozen=True)
class TriageReply:
    verdicts: Mapping[int, Tuple[TriageVerdict, str]] = field(default_factory=dict)
    cost: float = 0.0                 # USD, already billed to the account
    calls: int = 0                    # LLM calls made, the correction turn included


class DriveImageTriageAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._thinking = thinking or DRIVE_TRIAGE.thinking
        self._system: Optional[str] = None
        self._system_lock = asyncio.Lock()
        self._set_execution_context(execution_context)

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="DriveImageTriageAgent is not part of the delegation chain; use classify()")

    async def classify(self, images: Sequence[TriageInput], *, account_id: str) -> TriageReply:
        """One call (plus at most one correction turn), billed to `account_id` as one execution (§24.9)."""
        if not images:
            return TriageReply()
        counter = CallCounter()
        with self._execution_billing_scope(account_id) as ledger:
            try:
                verdicts = await self._classify(images, counter)
            except Exception as exc:
                mapped = with_usage(classify_model_error(exc, AGENT_TYPE), counter, ledger.cost())
                if mapped is exc:
                    raise
                raise mapped from exc
            finally:
                await self._flush_billing()
            return TriageReply(verdicts=verdicts, cost=ledger.cost(), calls=counter.calls)

    # -- implementation -----------------------------------------------------------------------------------------

    async def _classify(self, images: Sequence[TriageInput],
                        counter: CallCounter) -> Dict[int, Tuple[TriageVerdict, str]]:
        async with self._system_lock:
            if self._system is None:
                self._system = await build_drive_prompt(self._prompt_builder, AGENT_TYPE)
        parts: List[MessagePart] = []
        for image in images:
            parts.append(MessagePart(text=f"Image {image.index}: camera metadata "
                                          f"{'present' if image.camera_prior else 'absent'}"))
            parts.append(MessagePart(file_data={"base64": base64.b64encode(image.data).decode("ascii"),
                                                "mime_type": image.mime_type}))
        parts.append(MessagePart(text=_TASK.format(n=len(images))))
        history: List[Message] = [Message(role="user", parts=parts)]
        wanted = [image.index for image in images]
        verdicts: Dict[int, Tuple[TriageVerdict, str]] = {}
        for attempt in range(DRIVE_TRIAGE.max_parse_retries + 1):
            counter.calls += 1
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=self._system,
                messages=history,
                max_tokens=DRIVE_TRIAGE.max_tokens,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                thinking=self._thinking,
                allow_provider_storage=False,   # verbatim content of the user's files (§24.2)
                timeout=DRIVE_TRIAGE.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason in (FinishReason.MAX_TOKENS, FinishReason.SAFETY):
                raise DriveModelCallFailed(f"triage answer stopped: {response.finish_reason.value}")
            raw = response.text or ""
            try:
                for index, verdict in _parse(raw, set(wanted)).items():
                    verdicts.setdefault(index, verdict)
                problem = ""
            except ValueError as err:
                problem = f"was not valid ({err})"
                logger.warning(f"[{AGENT_TYPE}] invalid output (attempt {attempt + 1}): {err}")
            missing = [i for i in wanted if i not in verdicts]
            if not missing:
                return verdicts
            if not problem:
                problem = f"left out indexes {missing}"
                logger.warning(f"[{AGENT_TYPE}] answer left out {len(missing)} of {len(wanted)} images "
                               f"(attempt {attempt + 1})")
            if attempt >= DRIVE_TRIAGE.max_parse_retries:
                if not verdicts:
                    raise DriveModelCallFailed(f"no usable triage answer: {problem}")
                return verdicts
            history.append(Message(role="model", parts=[MessagePart(text=raw)]))
            history.append(Message(role="user", parts=[MessagePart(
                text=_CORRECTION.format(problem=problem, indexes=", ".join(str(i) for i in missing)))]))
        return verdicts  # pragma: no cover


def _parse(raw: str, wanted: set) -> Dict[int, Tuple[TriageVerdict, str]]:
    """Index -> (verdict, caption) for the wanted indexes; unknown indexes are ignored, the first answer per index
    wins. A malformed document or entry is a ValueError (the caller's correction turn)."""
    data = json.loads(raw)
    entries = data.get("images") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("`images` must be an array inside a JSON object")
    out: Dict[int, Tuple[TriageVerdict, str]] = {}
    for entry in entries:
        index, verdict, caption = _entry(entry)
        if index not in wanted:
            logger.warning(f"[{AGENT_TYPE}] answer names unknown index {index}; ignored")
            continue
        out.setdefault(index, (verdict, caption))
    return out


def _entry(entry: Any) -> Tuple[int, TriageVerdict, str]:
    if not isinstance(entry, dict) or isinstance(entry.get("index"), bool) or not isinstance(entry.get("index"), int):
        raise ValueError("every entry needs an integer `index`")
    try:
        verdict = TriageVerdict(entry.get("verdict"))
    except ValueError:
        raise ValueError(f"`verdict` must be text or photo, got {str(entry.get('verdict'))[:20]!r}") from None
    caption = ""
    if verdict is TriageVerdict.PHOTO:
        caption = " ".join(str(entry.get("caption") or "").split()[:MAX_CAPTION_WORDS])
    return entry["index"], verdict, caption
