"""
DriveProfilerAgent — describes one text file once, for the drive index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§25.4.1, §25.4.4, D15, D21).

Public interface:
    AGENT_TYPE = "drive_profiler"
    ListCaps(entities, values, dates, topics, words), FULL_CAPS, ListCaps.halved()
    ProfilerAnswer(profile, output_limited, model, usd, fallback_usd, calls)
    DriveProfilerAgent(config, execution_context, prompt_builder, thinking=None)
        .profile(request, *, key_values=True, caps=FULL_CAPS) -> ProfilerAnswer
            raises DriveModelCallFailed (DriveInputRefused / DriveInputTooLarge / DriveOutputInvalid) or
            DriveModelUnavailable, each carrying the calls made, their cost, the fallback's share and the model
            (`model_calls`, `model_cost`, `model_fallback_cost`, `model_name`)

In: the profiler input of `drive_profile.profile_input` (head, tail, heading lines, a collapsed folder's child names)
as data inside tags, the file name and kind, the previous labels on a re-profile ("keep unless the text
contradicts"), the list caps of this attempt. Out: an unverified `DriveProfile` (the caller runs
`drive_profile.verify`). The model never sees the file's path, folder or siblings (D21). Structured output per the
standard (src/adapters/CLAUDE.md): OUTPUT_FORMAT_DRIVE_PROFILER + response_schema + response_mime_type, json.loads,
one parse retry, no regex fallback. A reply stopped at the output limit is reported (`output_limited`) for the runner
to ask once more with halved caps; its truncated JSON is never parsed. No tools, history, biography or user
overrides; a missing prompt fails the call. Not a delegation agent (can_handle False); built per call by
`DriveProfilerRunner` (composition) with the user's configuration.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional, Tuple

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity
from ..domain.drive_enrichment_calls import ProfileRequest
from ..domain.drive_index_params import (
    ENTITY_FORMS_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX, PROFILE_KEY_VALUES_MAX, PROFILE_TOPICS_MAX,
    PROFILE_WORD_LIST_MAX,
)
from ..domain.drive_labels import DATE_LABELS, DOC_TYPES, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS
from ..domain.llm import FinishReason
from ..infrastructure.agent_config import DRIVE_PROFILER
from ..infrastructure.drive_model_call import (
    CallCounter, DriveOutputInvalid, build_drive_prompt, classify_model_error, ledger_spend, safety_stop, with_usage,
)
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "drive_profiler"


@dataclass(frozen=True)
class ListCaps:
    """The list caps one attempt asks for (§25.2.6); a reply stopped at the output cap is asked again halved
    (§25.4.4)."""
    entities: int = PROFILE_ENTITIES_MAX
    values: int = PROFILE_KEY_VALUES_MAX
    dates: int = PROFILE_DATES_MAX
    topics: int = PROFILE_TOPICS_MAX
    words: int = PROFILE_WORD_LIST_MAX

    def halved(self) -> "ListCaps":
        return ListCaps(entities=max(1, self.entities // 2), values=max(1, self.values // 2),
                        dates=max(1, self.dates // 2), topics=max(1, self.topics // 2),
                        words=max(1, self.words // 2))


FULL_CAPS = ListCaps()

_STR = {"type": "string"}
_WORDS = {"type": "array", "items": _STR}

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["title", "what_it_is", "language", "doc_type", "life_domains", "dates", "entities", "key_values",
                 "topics", "topics_native", "topic_words", "label_words", "domain_words", "validity_words"],
    "properties": {
        "title": {"type": "string", "description": "The document's own title, or a short one in its language."},
        "what_it_is": {"type": "string", "description": "One English sentence: what this file is."},
        "language": {"type": "string", "description": "The main language, ISO 639-1."},
        "doc_type": {"type": "string", "enum": list(DOC_TYPES), "description": "The closest document type."},
        "life_domains": {"type": "array", "items": {"type": "string", "enum": list(LIFE_DOMAINS)},
                         "description": "One to three life areas."},
        "dates": {"type": "array", "description": "Dates printed in the file.",
                  "items": {"type": "object", "required": ["label", "date", "printed"],
                            "properties": {"label": {"type": "string", "enum": list(DATE_LABELS)},
                                           "date": {"type": "string", "description": "YYYY-MM-DD."},
                                           "printed": {"type": "string",
                                                       "description": "The date exactly as printed in the file."}}}},
        "entities": {"type": "array", "description": "Named places, organisations, people, events, products, works.",
                     "items": {"type": "object", "required": ["name", "type", "printed", "forms"],
                               "properties": {
                                   "name": {"type": "string", "description": "Its usual name."},
                                   "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                                   "printed": {"type": "string", "description": "Exactly as printed in the file."},
                                   "forms": {**_WORDS, "description": "Other spellings: Latin, Cyrillic, local."}}}},
        "key_values": {"type": "array", "description": "Identifiers and amounts printed in the file.",
                       "items": {"type": "object", "required": ["label", "printed", "canonical"],
                                 "properties": {
                                     "label": {"type": "string", "enum": list(VALUE_LABELS)},
                                     "printed": {"type": "string", "description": "Exactly as printed."},
                                     "canonical": {"type": "string",
                                                   "description": "Digits and letters only; amounts as 1250.00."}}}},
        "topics": {**_WORDS, "description": "Subjects, in English."},
        "topics_native": {**_WORDS, "description": "The same subjects in the file's language."},
        "topic_words": {**_WORDS, "description": "Each topic in English, Spanish, Ukrainian and Russian."},
        "label_words": {**_WORDS, "description": "The document type's common names in the four languages, "
                                                 "singular and plural."},
        "domain_words": {**_WORDS, "description": "Common words for its life areas in the four languages."},
        "validity_words": {**_WORDS, "description": "Only when a validity or due date is printed: words for "
                                                    "'valid until' / 'expires' in the four languages."},
    },
}

_TASK = ("Write the profile of this file as the output format says. Limits: at most {entities} entities with at most "
         "{forms} forms each, {values} key values, {dates} dates, {topics} topics and {topics} native topics, and "
         "{words} words in each word list.")
_NO_KEY_VALUES = "This kind of file has no key values: return an empty key_values array."
_PREVIOUS = ("A previous profile of this file labelled it: doc_type {doc_type}; life_domains {domains}; valid until "
             "{valid}. Keep these labels unless the text contradicts them.")
_GAP = "[… the middle of the file is left out …]"
_CORRECTION = ("Your previous output was not valid: {error}. Output only the JSON object required by output_format: "
               "no markdown, no explanation.")


@dataclass(frozen=True)
class ProfilerAnswer:
    profile: Optional[DriveProfile]
    output_limited: bool = False      # stopped at the output cap: the runner asks again with halved caps (§25.4.4)
    model: str = ""                   # the model that served
    usd: float = 0.0                  # already billed to the account
    fallback_usd: float = 0.0         # the part the fallback served
    calls: int = 1


class DriveProfilerAgent(BaseAgent):

    def __init__(self, config: AgentConfig, execution_context: AgentExecutionContext,
                 prompt_builder: PromptBuilderPort, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._fallback_model = execution_context.fallback_model_name
        self._prompt_builder = prompt_builder
        self._thinking = thinking or DRIVE_PROFILER.thinking
        self._system: Optional[str] = None
        self._system_lock = asyncio.Lock()
        self._set_execution_context(execution_context)   # breaker accounting and the Gemini Flash fallback

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="DriveProfilerAgent is not part of the delegation chain; use profile()")

    async def profile(self, request: ProfileRequest, *, key_values: bool = True,
                      caps: ListCaps = FULL_CAPS) -> ProfilerAnswer:
        """One call (plus at most one parse retry), billed to `request.account_id` as one execution."""
        counter = CallCounter()
        with self._execution_billing_scope(request.account_id) as ledger:
            try:
                profile, limited = await self._profile(request, key_values, caps, counter)
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
            return ProfilerAnswer(profile=profile, output_limited=limited, model=spend.model, usd=spend.usd,
                                  fallback_usd=spend.fallback_usd, calls=counter.calls)

    # -- implementation -----------------------------------------------------------------------------------------

    async def _profile(self, request: ProfileRequest, key_values: bool, caps: ListCaps,
                       counter: CallCounter) -> Tuple[Optional[DriveProfile], bool]:
        async with self._system_lock:
            if self._system is None:
                self._system = await build_drive_prompt(self._prompt_builder, AGENT_TYPE)
        history: List[Message] = [Message(role="user", parts=[MessagePart(text=_message(request, key_values,
                                                                                        caps))])]
        for attempt in range(DRIVE_PROFILER.max_parse_retries + 1):
            counter.calls += 1
            response = await self._call_llm(LLMRequest(
                model_name=self._model_name,
                system_instruction=self._system,
                messages=history,
                max_tokens=DRIVE_PROFILER.max_tokens,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                thinking=self._thinking,
                allow_provider_storage=False,   # the user's file text (§25.6: store=false)
                timeout=DRIVE_PROFILER.call_timeout_s,
            ), turn=attempt + 1)
            if response.finish_reason is FinishReason.MAX_TOKENS:
                logger.warning(f"[{AGENT_TYPE}] output limit reached on item {request.item_id}")
                return None, True
            if response.finish_reason is FinishReason.SAFETY:
                raise safety_stop(response, AGENT_TYPE)
            raw = response.text or ""
            try:
                return _parse(raw, key_values, caps), False
            except ValueError as err:
                logger.warning(f"[{AGENT_TYPE}] invalid output on item {request.item_id} (attempt {attempt + 1}): "
                               f"{err}")
                if attempt >= DRIVE_PROFILER.max_parse_retries:
                    raise DriveOutputInvalid(f"invalid output after the parse retry: {err}") from err
                history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                history.append(Message(role="user", parts=[MessagePart(text=_CORRECTION.format(error=err))]))
        raise AssertionError("unreachable")  # pragma: no cover


def _message(request: ProfileRequest, key_values: bool, caps: ListCaps) -> str:
    inp = request.input
    lines = [f"File name: {request.name}", f"Kind: {request.kind}"]
    if request.previous is not None:
        prev = request.previous
        lines.append(_PREVIOUS.format(doc_type=prev.doc_type, domains=", ".join(prev.life_domains) or "none",
                                      valid=prev.valid_until.isoformat() if prev.valid_until else "none"))
    body = inp.head + (f"\n\n{_GAP}\n\n{inp.tail}" if inp.tail else "")
    lines.append(f"<document>\n{body}\n</document>")
    if inp.headings:
        lines.append("<headings>\n" + "\n".join(inp.headings) + "\n</headings>")
    if inp.child_names:
        lines.append("<folder_contents>\n" + "\n".join(inp.child_names) + "\n</folder_contents>")
    lines.append(_TASK.format(entities=caps.entities, forms=ENTITY_FORMS_MAX, values=caps.values, dates=caps.dates,
                              topics=caps.topics, words=caps.words))
    if not key_values:
        lines.append(_NO_KEY_VALUES)
    return "\n\n".join(lines)


def _parse(raw: str, key_values: bool, caps: ListCaps) -> DriveProfile:
    """The model's profile, unverified; lists cut to `caps`. A malformed document is a ValueError (the retry). A
    date that is not ISO is left out (interpretation must not cost the profile)."""
    data = json.loads(raw)   # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("the profile must be a JSON object")
    dates: List[ProfileDate] = []
    for entry in _objects(data, "dates"):
        iso = _iso(entry.get("date"))
        if iso is not None:
            dates.append(ProfileDate(label=_text(entry, "label"), iso=iso, printed=_text(entry, "printed")))
    entities = [ProfileEntity(name=_text(e, "name"), type=_text(e, "type"), printed=_text(e, "printed"),
                              forms=_strings(e, "forms")[:ENTITY_FORMS_MAX])
                for e in _objects(data, "entities")]
    values = [KeyValue(label=_text(v, "label"), printed=_text(v, "printed"), canonical=_text(v, "canonical"))
              for v in _objects(data, "key_values")] if key_values else []
    return DriveProfile(
        title=_text(data, "title"), what_it_is=_text(data, "what_it_is"), language=_text(data, "language"),
        doc_type=_text(data, "doc_type"), life_domains=_strings(data, "life_domains"),
        dates=tuple(dates[:caps.dates]), entities=tuple(entities[:caps.entities]),
        key_values=tuple(values[:caps.values]),
        topics=_strings(data, "topics")[:caps.topics], topics_native=_strings(data, "topics_native")[:caps.topics],
        topic_words=_strings(data, "topic_words")[:caps.words],
        label_words=_strings(data, "label_words")[:caps.words],
        domain_words=_strings(data, "domain_words")[:caps.words],
        validity_words=_strings(data, "validity_words")[:caps.words])


def _text(obj: Any, key: str) -> str:
    value = obj.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"`{key}` must be a string")
    return " ".join(value.split())


def _strings(obj: Any, key: str) -> Tuple[str, ...]:
    value = obj.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"`{key}` must be an array of strings")
    return tuple(s for s in (" ".join(v.split()) for v in value) if s)


def _objects(obj: Any, key: str) -> List[Any]:
    value = obj.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"`{key}` must be an array of objects")
    return value


def _iso(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        logger.warning(f"[{AGENT_TYPE}] date not ISO: {str(value)[:20]!r}; dropped")
        return None
