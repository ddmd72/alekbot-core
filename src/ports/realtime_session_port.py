from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict

from src.domain.voice_audio_frame import AudioFrame


@dataclass(frozen=True)
class RealtimeSessionEvent:
    """Normalized event out of a realtime session, provider JSON keys already
    translated away (RFC §4.4 - the port carries audio, not provider shapes)."""

    type: str
    payload: Dict[str, Any]


class RealtimeSessionPort(ABC):
    """One live speech-to-speech session with a realtime provider.
    PROMPT_CACHE_BOUNDARY must be stripped from `instructions` before open()
    (RFC §4.4 - an Anthropic-only cut point with no meaning in a realtime
    session prompt set once)."""

    @abstractmethod
    async def open(self, instructions: str, reasoning_effort: str, tools: list) -> None:
        """Connect and send the initial session.update. `instructions` must
        already have PROMPT_CACHE_BOUNDARY stripped by the caller."""

    @abstractmethod
    async def send_audio(self, frame: AudioFrame) -> None:
        """Push one inbound audio frame into the session."""

    @abstractmethod
    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        """Yield normalized events: audio_delta (payload: frame: AudioFrame,
        item_id - the provider's id for the assistant item this audio belongs to,
        needed to truncate it on barge-in),
        tool_call (payload: call_id, name, arguments), speech_started,
        speech_stopped, turn_committed (payload: item_id - the caller's turn is
        now a conversation item; the provider does not reply on its own, the
        caller of this port starts the reply), response_created, response_done (payload: usage -
        provider-native token usage dict, model - the provider's own model
        id, so callers can label usage without hardcoding a provider-specific
        model string), user_transcript (payload: text - final transcript of
        the caller's speech), model_transcript (payload: text - final
        transcript of the model's spoken response), error (payload: message)."""

    @abstractmethod
    async def submit_tool_result(self, call_id: str, output: str) -> None:
        """Submit a function_call_output for call_id, including late (RFC
        §4.7). Does not itself call request_response()."""

    @abstractmethod
    async def submit_message(self, role: str, text: str) -> None:
        """Inject a fresh conversation item not tied to any tool call - the
        resolve_late_answer fresh-message branch (RFC §4.7)."""

    @abstractmethod
    async def request_response(self) -> None:
        """Trigger response.create. Caller is responsible for not calling
        this while a response is already active (RFC §4.7 corner-case table)."""

    @abstractmethod
    async def cancel_response(self) -> None:
        """Cancel the in-flight response (barge-in). Caller is responsible for
        not calling this when no response is active (RFC §4.7 corner-case
        table; the provider errors on a cancel with nothing active)."""

    @abstractmethod
    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Cut the assistant item down to the audio the caller actually heard, so
        the model's context holds what was said, not what was generated. Must not
        exceed the item's generated audio (the provider errors)."""

    @abstractmethod
    async def close(self) -> None:
        """Close the session. No retry, no reconnect (RFC §4.14)."""
