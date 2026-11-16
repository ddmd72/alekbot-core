"""
The request to keep a web call's text as one file (VOICE_CALL_LIVE_TEXT_RFC §4.11), and its parsing.

The page sends the call's lines it holds in memory: a translation pair `{source, translation, interrupted?}`, or a
Lelik block `{question, answer}`. Parsing is strict: a body of any other shape is refused whole, never repaired.

This module holds the request's small value objects together (the one-public-class rule is waived on purpose:
the entry, the request, the outcome and the two errors are one concept).
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union

from .voice_mode import VOICE_MODES_WITH_CALL_FILE, VoiceMode

MAX_CALL_TEXT_BODY_BYTES = 512 * 1024
MAX_CALL_TEXT_ENTRIES = 2000
MAX_CALL_DURATION_S = 6 * 3600

CALL_TEXT_TARGETS = frozenset({"drive", "chat"})

_PAIR_KEYS = frozenset({"source", "translation"})
_PAIR_OPTIONAL = frozenset({"interrupted"})
_LELIK_KEYS = frozenset({"question", "answer"})

# The summarizer turn's `kind` (its prompts name both values): a line heard on the call and Lelik's line after it
# (a translation on the interpreter page, the teacher's reply on the tutor page), or an "Ask Lelik" block.
TURN_KIND_EXCHANGE = "exchange"
TURN_KIND_ASK = "ask"


@dataclass(frozen=True)
class CallTextEntry:
    """One line of the file: what was said (`source`) and the reply (`text`); `lelik` marks a question/answer block."""

    source: str
    text: str
    interrupted: bool = False
    lelik: bool = False


@dataclass(frozen=True)
class CallTextRequest:
    target: str
    mode: VoiceMode
    duration_s: float
    entries: Tuple[CallTextEntry, ...]


class CallTextTooLarge(ValueError):
    """The request holds more entries than the cap."""


@dataclass(frozen=True)
class CallTextFileOutcome:
    """`reason` is one of not_connected | refused | no_chat | failed when `saved` is False."""

    saved: bool
    path: str = ""
    reason: str = ""


def _entry(raw: Any) -> CallTextEntry:
    if not isinstance(raw, dict):
        raise ValueError("entry is not an object")
    keys = set(raw)
    if _LELIK_KEYS == keys:
        question, answer = raw["question"], raw["answer"]
        if not isinstance(question, str) or not isinstance(answer, str):
            raise ValueError("entry texts must be strings")
        return CallTextEntry(source=question, text=answer, lelik=True)
    if _PAIR_KEYS <= keys <= (_PAIR_KEYS | _PAIR_OPTIONAL):
        source, translation = raw["source"], raw["translation"]
        interrupted = raw.get("interrupted", False)
        if not isinstance(source, str) or not isinstance(translation, str):
            raise ValueError("entry texts must be strings")
        if not isinstance(interrupted, bool):
            raise ValueError("interrupted must be a boolean")
        return CallTextEntry(source=source, text=translation, interrupted=interrupted)
    raise ValueError("entry has neither shape")


def parse_call_text_request(body: Any) -> CallTextRequest:
    """The request a page body describes. `CallTextTooLarge` for too many entries; `ValueError` for anything malformed."""
    if not isinstance(body, dict):
        raise ValueError("body is not an object")
    target = body.get("target")
    if target not in CALL_TEXT_TARGETS:
        raise ValueError("unknown target")
    try:
        mode = VoiceMode(body.get("mode"))
    except ValueError:
        raise ValueError("unknown mode") from None
    if mode not in VOICE_MODES_WITH_CALL_FILE:
        raise ValueError("mode has no call file")
    duration = body.get("duration_s")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= MAX_CALL_DURATION_S:
        raise ValueError("duration_s out of range")
    entries = body.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("entries must be a non-empty list")
    if len(entries) > MAX_CALL_TEXT_ENTRIES:
        raise CallTextTooLarge(f"{len(entries)} entries")
    return CallTextRequest(target=target, mode=mode, duration_s=float(duration),
                           entries=tuple(_entry(raw) for raw in entries))


def summarizer_turns(entries: Tuple[CallTextEntry, ...]) -> List[Dict[str, Union[str, bool]]]:
    """The summarizer's input contract: `request_text` is what was said, `response_text` Lelik's line after it, and
    `kind` which of the two shapes the turn is; an exchange also says whether Lelik's line was cut off."""
    turns: List[Dict[str, Union[str, bool]]] = []
    for e in entries:
        if e.lelik:
            turns.append({"kind": TURN_KIND_ASK, "request_text": e.source, "response_text": e.text})
        else:
            turns.append({"kind": TURN_KIND_EXCHANGE, "request_text": e.source, "response_text": e.text,
                          "interrupted": e.interrupted})
    return turns
