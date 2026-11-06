"""Shared fakes of the Smart/Quick turn-key tests (SYSTEM_TURN_FINGERPRINT_RFC §4.3, §4.8)."""
import json
from dataclasses import dataclass, field
from typing import Any, List

from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, Message
from src.domain.system_fingerprint import render_session_key, scrub
from tests.support.tool_e2e import StubPromptBuilder

KEY = "lamp-orbit-fern"
KEY2 = "cedar-kayak-tulip"


class KeyedPromptBuilder(StubPromptBuilder):
    """A builder with the assembly's semantics for the one block: `session_key` renders last in the prompt."""

    async def build_for_agent(self, **kwargs: Any) -> str:  # type: ignore[override]
        self.calls.append(kwargs)
        out = "SYSTEM\n" + PROMPT_CACHE_BOUNDARY + "\n"
        if kwargs.get("session_key"):
            out += render_session_key(kwargs["session_key"])
        return out


@dataclass
class _Session:
    history: List[Message] = field(default_factory=list)


class HistorySessions:
    """A session store whose `load_session` returns an object with `.history`."""

    def __init__(self, history: List[Message]) -> None:
        self._history = history

    async def load_session(self, session_id: str) -> _Session:
        return _Session(list(self._history))


class KeySource:
    """Returns the given keys in order and counts the draws."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.keys[self.calls - 1]


def terminal_args() -> dict:
    """A Claude-shape deliver_response whose every field holds a spelling of KEY."""
    return {"response": json.dumps({
        "full_response": "Answer lamp-orbit-fern here.",
        "response_summary": "Summary Lamp Orbit Fern.",
        "rich_content": {"type": "table", "data": {"rows": [{"cells": ["cell lamp.orbit.fern"]}]},
                         "fallback": "fallback LAMP-ORBIT-FERN"},
        "link_list": [{"anchor": "1", "title": "Tom Lamp Orbit Fern", "url": "https://example.com/docs"}],
    })}


def holds_key(value: Any, key: str = KEY) -> bool:
    return scrub(repr(value), key)[1] > 0


def system_text(request: LLMRequest) -> str:
    return request.system_instruction or ""


def last_user_parts(request: LLMRequest) -> List[str]:
    return [p.text or "" for p in request.messages[-1].parts]
