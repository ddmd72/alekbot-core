"""
DriveTranscriberAgent — builds the batch request for one audio window and parses its answer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5, Δ102, G28, G36).

Public interface:
    AGENT_TYPE = "drive_transcriber"
    DriveTranscriberAgent(config, thinking=None)
        .build_request(window, *, prompt, key, file_name, file_mime) -> BatchRequest
        .parse(text) -> WindowTranscript            raises DriveOutputInvalid (the window's failed attempt)

In: a window of the recording (its length as a `MM:SS` string, the uploaded provider file by name) and the Firestore
prompt (`build_drive_prompt`: tokens COGNITIVE_PROCESS_DRIVE_TRANSCRIBER + OUTPUT_FORMAT_DRIVE_TRANSCRIBER). Out: a
`WindowTranscript`. Structured output per the standard (src/adapters/CLAUDE.md): a response schema, `json.loads` once,
a strict check of every field, no regex fallback; whatever does not pass is `DriveOutputInvalid`.

Honest difference from the other drive agents (Δ102): a batch is not a call, so nothing goes through
`BaseAgent._call_llm` — no retry, failover, `TokenLedger`, prompt-content row or Logfire span (G28). Each collected
result is priced by `BatchUsage.cost()` and billed directly by `DriveTranscriptRunner` (G36). Not a delegation agent
(`can_handle` False); no tools, history or user overrides. The agent never imports `google.genai`: the response schema
is a plain mapping that the batch adapter turns into the provider's typed schema.
"""
from __future__ import annotations

import json
import math
from typing import Any, List, Optional

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.batch_transcription import BatchRequest
from ..domain.drive_transcript import Segment, WindowSpec, WindowTranscript
from ..domain.drive_transcript_time import format_stamp, parse_stamp
from ..infrastructure.agent_config import DRIVE_TRANSCRIBER
from ..infrastructure.drive_model_call import DriveOutputInvalid
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "drive_transcriber"

# Adapter-agnostic (lowercase JSON-schema types): the batch adapter converts it. `speaker` is nullable the SDK way
# (`nullable`), not as a type list, which the provider's typed schema rejects.
_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["language_codes", "speakers", "no_speech", "segments"],
    "properties": {
        "language_codes": {"type": "array", "items": {"type": "string"},
                           "description": "ISO 639-1 codes of the languages spoken, the most spoken first."},
        "speakers": {"type": "integer", "description": "How many distinct voices; 1 for one voice, 0 for no speech."},
        "no_speech": {"type": "boolean", "description": "True only when the window holds no intelligible speech."},
        "segments": {
            "type": "array", "description": "The speech in order of time.",
            "items": {"type": "object", "required": ["start", "speaker", "text"], "properties": {
                "start": {"type": "string",
                          "description": "Where the segment starts in this window: MM:SS, or H:MM:SS from one hour."},
                "speaker": {"type": "integer", "nullable": True,
                            "description": "Voice number from 1, stable in this window; null for a single speaker."},
                "text": {"type": "string", "description": "The words exactly as spoken."},
            }},
        },
    },
}

_TASK = ("Transcribe this audio window as the output format says. The window is {length} long; every start is counted "
         "from the beginning of the window and must stay within {length}. Transcribe it to its end.")


class DriveTranscriberAgent(BaseAgent):

    def __init__(self, config: AgentConfig, thinking: Optional[str] = None) -> None:
        super().__init__(config)
        self._thinking = thinking or DRIVE_TRANSCRIBER.thinking

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                     error="DriveTranscriberAgent is not part of the delegation chain; use "
                                           "build_request() and parse()")

    def build_request(self, window: WindowSpec, *, prompt: str, key: str, file_name: str,
                      file_mime: str) -> BatchRequest:
        """The request of one window. The length is rounded up, so the model is never told a shorter window than the
        audio it holds. `file_name` is the provider's file name (`files/<id>`), never a URL."""
        length = format_stamp(math.ceil(window.duration_s))
        return BatchRequest(key=key, system_instruction=prompt, user_text=_TASK.format(length=length),
                            response_schema=_RESPONSE_SCHEMA, file_name=file_name, file_mime=file_mime,
                            thinking_level=self._thinking, max_output_tokens=DRIVE_TRANSCRIBER.max_tokens)

    def parse(self, text: Optional[str]) -> WindowTranscript:
        """The window's transcript. `json.loads` once and a strict check; anything else is DriveOutputInvalid (the
        window's failed attempt, §27.5). The text is never logged: it is other people's speech."""
        try:
            return _parse(text or "")
        except ValueError as err:
            logger.warning(f"[{AGENT_TYPE}] invalid output ({len(text or '')} chars): {err}")
            raise DriveOutputInvalid(f"invalid transcript output: {err}") from err


def _parse(raw: str) -> WindowTranscript:
    data = json.loads(raw)    # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("the answer must be a JSON object")
    codes = _field(data, "language_codes")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise ValueError("`language_codes` must be an array of strings")
    speakers = _field(data, "speakers")
    if not _is_int(speakers) or speakers < 0:
        raise ValueError("`speakers` must be a non-negative integer")
    no_speech = _field(data, "no_speech")
    if not isinstance(no_speech, bool):
        raise ValueError("`no_speech` must be a boolean")
    raw_segments = _field(data, "segments")
    if not isinstance(raw_segments, list):
        raise ValueError("`segments` must be an array")
    segments: List[Segment] = []
    for entry in raw_segments:
        if not isinstance(entry, dict):
            raise ValueError("a segment must be an object")
        start = _field(entry, "start")
        if not isinstance(start, str):
            raise ValueError("`start` must be a MM:SS string, never a number")
        speaker = _field(entry, "speaker")
        if speaker is not None and (not _is_int(speaker) or speaker < 0):
            raise ValueError("`speaker` must be an integer or null")
        text = _field(entry, "text")
        if not isinstance(text, str):
            raise ValueError("`text` must be a string")
        segments.append(Segment(start_s=parse_stamp(start), speaker=speaker, text=text))
    return WindowTranscript(language_codes=tuple(codes), speakers=speakers, no_speech=no_speech,
                            segments=tuple(segments))


def _field(obj: dict, key: str) -> Any:
    if key not in obj:
        raise ValueError(f"`{key}` is missing")
    return obj[key]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
