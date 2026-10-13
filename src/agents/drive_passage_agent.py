"""
DrivePassageAgent — passage hints (doc2query) for a few chunks of one file, for the drive index
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.2, §25.4.4, D17, P8).

Public interface:
    AGENT_TYPE = "drive_passages"
    PassageAnswer(items_by_index, missing, output_limited, model, usd, fallback_usd, calls)
    DrivePassageAgent(config, execution_context, prompt_builder, thinking=None)
        .hints(chunks, *, title, what_it_is, account_id) -> PassageAnswer
            raises DriveModelCallFailed (DriveInputRefused / DriveInputTooLarge / DriveOutputInvalid) or
            DriveModelUnavailable, each carrying the calls made, their cost, the fallback's share and the model

Each chunk goes under an opaque index 1..n (never its chunk index), with the profile's title and what_it_is for
context. Out, per chunk: keywords (subjects, names, synonyms, translations in en, es, fr, uk, ru) and questions, each
with an evidence quote copied from that chunk — ungrounded (the caller runs `drive_hints.ground`, which also enforces
the caps). Mapping back by index: an unknown index is ignored, a repeated one keeps its first answer, and a reply with
indexes missing gets one correction turn; what is still missing is reported in `missing`. Structured output per the
standard: OUTPUT_FORMAT_DRIVE_PASSAGES + response_schema + response_mime_type, json.loads, no regex fallback. A reply
stopped at the output limit is reported (`output_limited`) for the runner to split the batch; its truncated JSON is
never parsed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.drive_enrichment import HintItem, HintType
from ..domain.drive_index_params import HINT_EVIDENCE_WORDS_MAX, HINT_KEYWORDS_MAX, HINT_QUESTIONS_MAX
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import DRIVE_PASSAGES
from ..infrastructure.drive_model_call import (
    CallCounter, DriveOutputInvalid, build_drive_prompt, classify_model_error, ledger_spend, safety_stop, with_usage,
)
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "drive_passages"
_ITEMS_READ_MAX = 2 * (HINT_KEYWORDS_MAX + HINT_QUESTIONS_MAX)   # bound on what one passage's answer is read for

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["passages"],
    "properties": {
        "passages": {
            "type": "array",
            "description": "One entry per passage, by its index.",
            "items": {
                "type": "object",
                "required": ["index", "items"],
                "properties": {
                    "index": {"type": "integer", "description": "The passage's index as given."},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["type", "text", "evidence"],
                            "properties": {
                                "type": {"type": "string",
                                         "enum": [HintType.KEYWORD.value, HintType.QUESTION.value]},
                                "text": {"type": "string",
                                         "description": "keyword: a word or short phrase; question: one question "
                                                        "the passage answers."},
                                "evidence": {"type": "string",
                                             "description": "Up to 12 words copied exactly from this passage."},
                            },
                        },
                    },
                },
            },
        },
    },
}

_TASK = ("Write the keywords and questions for each of the {n} passages above, as the output format says: at most "
         "{keywords} keywords and {questions} questions per passage, each with an evidence quote of at most "
         "{words} words copied exactly from that passage.")
_CORRECTION = ("Your previous output {problem}. Output only the JSON object required by output_format, with one "
               "entry for every passage: {indexes}.")


@dataclass(frozen=True)
class PassageAnswer:
    items_by_index: Mapping[int, Tuple[HintItem, ...]] = field(default_factory=dict)   # by chunk index
    missing: Tuple[int, ...] = ()       # chunk indexes still unanswered after the correction turn
    output_limited: bool = False        # stopped at the output cap: the runner splits the batch (§25.4.4)
    model: str = ""
    usd: float = 0.0
    fallback_usd: float = 0.0
    calls: int = 0


class DrivePassageAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._fallback_model = execution_context.fallback_model_name
        self._prompt_builder = prompt_builder
        self._thinking = thinking or DRIVE_PASSAGES.thinking
        self._system: Optional[str] = None
        self._system_lock = asyncio.Lock()
        self._set_execution_context(execution_context)

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="DrivePassageAgent is not part of the delegation chain; use hints()")

    async def hints(self, chunks: Sequence[Tuple[int, str]], *, title: str, what_it_is: str,
                    account_id: str) -> PassageAnswer:
        """One call (plus at most one correction turn), billed to `account_id` as one execution."""
        if not chunks:
            return PassageAnswer()
        counter = CallCounter()
        with self._execution_billing_scope(account_id) as ledger:
            try:
                items, missing, limited = await self._hints(chunks, title, what_it_is, counter)
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
            return PassageAnswer(items_by_index=items, missing=missing, output_limited=limited, model=spend.model,
                                 usd=spend.usd, fallback_usd=spend.fallback_usd, calls=counter.calls)

    # -- implementation -----------------------------------------------------------------------------------------

    async def _hints(self, chunks: Sequence[Tuple[int, str]], title: str, what_it_is: str, counter: CallCounter
                     ) -> Tuple[Dict[int, Tuple[HintItem, ...]], Tuple[int, ...], bool]:
        async with self._system_lock:
            if self._system is None:
                self._system = await build_drive_prompt(self._prompt_builder, AGENT_TYPE)
        by_label = {label: index for label, (index, _) in enumerate(chunks, start=1)}
        history: List[Message] = [Message(role="user", parts=[MessagePart(text=_message(chunks, title,
                                                                                        what_it_is))])]
        answered: Dict[int, Tuple[HintItem, ...]] = {}
        for attempt in range(DRIVE_PASSAGES.max_parse_retries + 1):
            counter.calls += 1
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=self._system,
                messages=history,
                max_tokens=DRIVE_PASSAGES.max_tokens,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                thinking=self._thinking,
                allow_provider_storage=False,   # the user's file text (§25.6: store=false)
                timeout=DRIVE_PASSAGES.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[{AGENT_TYPE}] output limit reached on {len(chunks)} passages")
                return {by_label[k]: v for k, v in answered.items()}, (), True
            if response.finish_reason is FinishReason.SAFETY:
                raise safety_stop(response, AGENT_TYPE)
            raw = response.text or ""
            try:
                for label, items in _parse(raw, set(by_label)).items():
                    answered.setdefault(label, items)
                problem = ""
            except ValueError as err:
                problem = f"was not valid ({err})"
                logger.warning(f"[{AGENT_TYPE}] invalid output (attempt {attempt + 1}): {err}")
            missing = [label for label in by_label if label not in answered]
            if not missing:
                return {by_label[k]: v for k, v in answered.items()}, (), False
            if not problem:
                problem = f"left out passages {missing}"
            if attempt >= DRIVE_PASSAGES.max_parse_retries:
                if not answered:
                    raise DriveOutputInvalid(f"no usable passage answer: {problem}")
                return ({by_label[k]: v for k, v in answered.items()}, tuple(by_label[m] for m in missing), False)
            history.append(Message(role="model", parts=[MessagePart(text=raw)]))
            history.append(Message(role="user", parts=[MessagePart(
                text=_CORRECTION.format(problem=problem, indexes=", ".join(str(m) for m in missing)))]))
        raise AssertionError("unreachable")  # pragma: no cover


def _message(chunks: Sequence[Tuple[int, str]], title: str, what_it_is: str) -> str:
    about = " — ".join(p for p in (title.strip(), what_it_is.strip()) if p)
    lines = [f"The file: {about}" if about else "The file has no description yet."]
    for label, (_, text) in enumerate(chunks, start=1):
        lines.append(f'<passage n="{label}">\n{text}\n</passage>')
    lines.append(_TASK.format(n=len(chunks), keywords=HINT_KEYWORDS_MAX, questions=HINT_QUESTIONS_MAX,
                              words=HINT_EVIDENCE_WORDS_MAX))
    return "\n\n".join(lines)


def _parse(raw: str, wanted: set) -> Dict[int, Tuple[HintItem, ...]]:
    """Label -> items for the wanted labels; unknown labels are ignored, the first answer per label wins. A
    malformed document or entry is a ValueError (the caller's correction turn)."""
    data = json.loads(raw)
    entries = data.get("passages") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("`passages` must be an array inside a JSON object")
    out: Dict[int, Tuple[HintItem, ...]] = {}
    for entry in entries:
        label, items = _entry(entry)
        if label not in wanted:
            logger.warning(f"[{AGENT_TYPE}] answer names unknown passage {label}; ignored")
            continue
        out.setdefault(label, items)
    return out


def _entry(entry: Any) -> Tuple[int, Tuple[HintItem, ...]]:
    if not isinstance(entry, dict) or isinstance(entry.get("index"), bool) or not isinstance(entry.get("index"), int):
        raise ValueError("every passage needs an integer `index`")
    raw_items = entry.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("`items` must be an array")
    items: List[HintItem] = []
    for item in raw_items[:_ITEMS_READ_MAX]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("type", "text", "evidence")):
            raise ValueError("every item needs string `type`, `text` and `evidence`")
        items.append(HintItem(text=item["text"].strip(), type=item["type"].strip(), evidence=item["evidence"]))
    return entry["index"], tuple(items)
