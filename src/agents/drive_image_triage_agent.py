"""
DriveImageTriageAgent — sorts a batch of thumbnails into text to read and photographs, and describes each photograph
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.5; triage v2: §25.4.3, §25.4.4, D20, OQ-V1).

Public interface:
    AGENT_TYPE = "drive_triage"
    TriageInput(index, data, mime_type, camera_prior)
    TriageAnswer(results: Mapping[int, (TriageVerdict, PhotoDescription | None)], missing, output_limited, model,
                 usd, fallback_usd, calls)
    DriveImageTriageAgent(config, execution_context, prompt_builder, thinking=None)
        .classify(images, account_id=) -> TriageAnswer
            raises DriveModelCallFailed (DriveInputRefused / DriveInputTooLarge / DriveOutputInvalid) or
            DriveModelUnavailable, each carrying the calls made, their cost, the fallback's share and the model

Each thumbnail goes after its own text label "Image <n>: camera metadata present|absent" (an opaque index, never the
item id), and the answer is mapped back by index: an unknown index is ignored, a repeated one keeps its first answer.
A photograph gets a description (at most PHOTO_DESCRIPTION_CHARS), objects with up to PHOTO_OBJECT_FORMS_MAX spellings
each, a scene of a few words and the visible text, all automatic; a text image gets none. The caption-shift check
(OQ-V1): byte-identical images in one batch must get the same verdict — when they do not, the answer is shifted, so
their answers are dropped and asked again with the correction turn, which also asks for any index left out; images
still missing or still disagreeing after it are reported in `missing` (unsettled, retried). Structured output per the
standard: OUTPUT_FORMAT_DRIVE_TRIAGE + response_schema + response_mime_type, json.loads, no regex fallback. A reply
stopped at the output limit is reported (`output_limited`) for the runner to split the batch.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.drive_enrichment import PhotoDescription, PhotoObject
from ..domain.drive_index import TriageVerdict
from ..domain.drive_index_params import (
    PHOTO_DESCRIPTION_CHARS, PHOTO_OBJECT_FORMS_MAX, PHOTO_OBJECTS_MAX, PHOTO_SCENE_WORDS, PHOTO_VISIBLE_TEXT_CHARS,
)
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import DRIVE_TRIAGE
from ..infrastructure.drive_model_call import (
    CallCounter, DriveInputRefused, DriveOutputInvalid, build_drive_prompt, classify_model_error, ledger_spend,
    with_usage,
)
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "drive_triage"

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "description": "One entry per image, by its index.",
            "items": {
                "type": "object",
                "required": ["index", "verdict", "description", "objects", "scene", "visible_text"],
                "properties": {
                    "index": {"type": "integer", "description": "The image's index as given."},
                    "verdict": {"type": "string", "enum": [TriageVerdict.TEXT.value, TriageVerdict.PHOTO.value],
                                "description": "text: the image is there to be read; photo: everything else."},
                    "description": {"type": "string",
                                    "description": "photo: one or two English sentences on what it shows; text: "
                                                   "empty."},
                    "objects": {"type": "array", "description": "photo: the main things and landmarks; text: empty.",
                                "items": {"type": "object", "required": ["name", "forms"],
                                          "properties": {
                                              "name": {"type": "string", "description": "In English."},
                                              "forms": {"type": "array", "items": {"type": "string"},
                                                        "description": "The name in Spanish, Ukrainian, Russian "
                                                                       "and French."}}}},
                    "scene": {"type": "string", "description": "photo: a few English words for the setting."},
                    "visible_text": {"type": "string",
                                     "description": "photo: words legible in it, as written; else empty."},
                },
            },
        },
    },
}

_TASK = "Classify each of the {n} images above by its index, and describe each photograph, as the output format says."
_CORRECTION = ("Your previous output {problem}. Output only the JSON object required by output_format, with one "
               "entry for every index: {indexes}.")

_Result = Tuple[TriageVerdict, Optional[PhotoDescription]]


@dataclass(frozen=True)
class TriageInput:
    index: int
    data: bytes
    mime_type: str
    camera_prior: bool


@dataclass(frozen=True)
class TriageAnswer:
    results: Mapping[int, _Result] = field(default_factory=dict)
    missing: Tuple[int, ...] = ()     # unanswered or still disagreeing after the correction turn: unsettled
    output_limited: bool = False      # stopped at the output cap: the runner splits the batch (§25.4.4)
    model: str = ""
    usd: float = 0.0                  # already billed to the account
    fallback_usd: float = 0.0
    calls: int = 0                    # LLM calls made, the correction turn included


class DriveImageTriageAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._fallback_model = execution_context.fallback_model_name
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

    async def classify(self, images: Sequence[TriageInput], *, account_id: str) -> TriageAnswer:
        """One call (plus at most one correction turn), billed to `account_id` as one execution (§24.9)."""
        if not images:
            return TriageAnswer()
        counter = CallCounter()
        with self._execution_billing_scope(account_id) as ledger:
            try:
                results, missing, limited = await self._classify(images, counter)
            except Exception as exc:
                spend = ledger_spend(ledger, primary_model=self._model_name, fallback_model=self._fallback_model)
                mapped = with_usage(classify_model_error(exc, AGENT_TYPE), counter, spend.usd,
                                    fallback_cost=spend.fallback_usd, model=spend.model)
                if mapped is exc:
                    raise
                raise mapped from exc
            finally:
                await self._flush_billing()
            spend = ledger_spend(ledger, primary_model=self._model_name, fallback_model=self._fallback_model)
            return TriageAnswer(results=results, missing=missing, output_limited=limited, model=spend.model,
                                usd=spend.usd, fallback_usd=spend.fallback_usd, calls=counter.calls)

    # -- implementation -----------------------------------------------------------------------------------------

    async def _classify(self, images: Sequence[TriageInput], counter: CallCounter
                        ) -> Tuple[Dict[int, _Result], Tuple[int, ...], bool]:
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
        twins = _identical_groups(images)
        results: Dict[int, _Result] = {}
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
                allow_provider_storage=False,   # thumbnails of the user's files (§24.2, §25.6)
                timeout=DRIVE_TRIAGE.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[{AGENT_TYPE}] output limit reached on {len(images)} images")
                return {}, (), True
            if response.finish_reason is FinishReason.SAFETY:
                raise DriveInputRefused("the provider's safety filter stopped the answer")
            raw = response.text or ""
            problems: List[str] = []
            try:
                for index, result in _parse(raw, set(wanted)).items():
                    results.setdefault(index, result)
            except ValueError as err:
                problems.append(f"was not valid ({err})")
                logger.warning(f"[{AGENT_TYPE}] invalid output (attempt {attempt + 1}): {err}")
            shifted = _disagreeing(twins, results)
            for index in shifted:
                results.pop(index, None)
            missing = [i for i in wanted if i not in results]
            if not missing:
                return results, (), False
            if shifted:
                problems.append(f"gave different verdicts to identical images {sorted(shifted)}")
                logger.warning(f"[{AGENT_TYPE}] identical images {sorted(shifted)} got different verdicts "
                               f"(attempt {attempt + 1}): the answer may be shifted")
            left_out = [i for i in missing if i not in shifted]
            if left_out:
                problems.append(f"left out indexes {left_out}")
            if attempt >= DRIVE_TRIAGE.max_parse_retries:
                if not results:
                    raise DriveOutputInvalid(f"no usable triage answer: {'; '.join(problems)}")
                return results, tuple(missing), False
            history.append(Message(role="model", parts=[MessagePart(text=raw)]))
            history.append(Message(role="user", parts=[MessagePart(text=_CORRECTION.format(
                problem="; ".join(problems), indexes=", ".join(str(i) for i in missing)))]))
        raise AssertionError("unreachable")  # pragma: no cover


def _identical_groups(images: Sequence[TriageInput]) -> List[List[int]]:
    by_digest: Dict[str, List[int]] = {}
    for image in images:
        by_digest.setdefault(hashlib.sha256(image.data).hexdigest(), []).append(image.index)
    return [group for group in by_digest.values() if len(group) > 1]


def _disagreeing(groups: Sequence[Sequence[int]], results: Mapping[int, _Result]) -> List[int]:
    out: List[int] = []
    for group in groups:
        verdicts = {results[i][0] for i in group if i in results}
        if len(verdicts) > 1:
            out.extend(group)
    return out


def _parse(raw: str, wanted: set) -> Dict[int, _Result]:
    """Index -> (verdict, photo) for the wanted indexes; unknown indexes are ignored, the first answer per index
    wins. A malformed document or entry is a ValueError (the caller's correction turn)."""
    data = json.loads(raw)
    entries = data.get("images") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("`images` must be an array inside a JSON object")
    out: Dict[int, _Result] = {}
    for entry in entries:
        index, result = _entry(entry)
        if index not in wanted:
            logger.warning(f"[{AGENT_TYPE}] answer names unknown index {index}; ignored")
            continue
        out.setdefault(index, result)
    return out


def _entry(entry: Any) -> Tuple[int, _Result]:
    if not isinstance(entry, dict) or isinstance(entry.get("index"), bool) or not isinstance(entry.get("index"), int):
        raise ValueError("every entry needs an integer `index`")
    try:
        verdict = TriageVerdict(entry.get("verdict"))
    except ValueError:
        raise ValueError(f"`verdict` must be text or photo, got {str(entry.get('verdict'))[:20]!r}") from None
    if verdict is TriageVerdict.TEXT:
        return entry["index"], (verdict, None)
    objects: List[PhotoObject] = []
    raw_objects = entry.get("objects") or []
    if not isinstance(raw_objects, list):
        raise ValueError("`objects` must be an array")
    for obj in raw_objects[:PHOTO_OBJECTS_MAX]:
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise ValueError("every object needs a string `name`")
        forms = obj.get("forms") or []
        if not isinstance(forms, list):
            raise ValueError("`forms` must be an array")
        name = " ".join(obj["name"].split())
        if name:
            objects.append(PhotoObject(name=name, forms=tuple(
                f for f in (" ".join(str(x).split()) for x in forms) if f)[:PHOTO_OBJECT_FORMS_MAX]))
    photo = PhotoDescription(
        description=" ".join(_string(entry, "description").split())[:PHOTO_DESCRIPTION_CHARS],
        objects=tuple(objects),
        scene=" ".join(_string(entry, "scene").split()[:PHOTO_SCENE_WORDS]),
        visible_text=" ".join(_string(entry, "visible_text").split())[:PHOTO_VISIBLE_TEXT_CHARS])
    return entry["index"], (verdict, photo)


def _string(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"`{key}` must be a string")
    return value
