"""
Per-turn helpers of the system-turn fingerprint for the orchestrators (docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md
§4.3, §4.6): neutralise system-tag look-alikes in loaded history, scrub the key from the parsed reply, and key a
header whose body lines stay outside the tag. Imports domain only; the key is a plain argument, never stored.

Public interface:
    neutralize_history(messages) -> (messages, count)      look-alikes in part texts lose their opener; no mutation
    scrub_value(value, key) -> (value, count)              recursive `scrub` over str / dict / list / RichContent
    scrub_reply(response, summary, key) -> (response, summary, count)
    keyed_header(header, lines, key) -> str                `wrap_system(header, key)` + neutralised lines
"""
from __future__ import annotations

import dataclasses
from typing import Any, List, Optional, Tuple

from ..domain.llm import Message
from ..domain.messaging import RichContent, SmartResponse
from ..domain.system_fingerprint import scrub, wrap_system
from ..domain.untrusted_text import neutralize_system_tag

__all__ = ["neutralize_history", "scrub_value", "scrub_reply", "keyed_header"]


def neutralize_history(messages: List[Message]) -> Tuple[List[Message], int]:
    """A new list; a changed part/message is replaced via `model_copy`, unchanged ones are the same objects. The input
    list and its parts are never mutated (the handler persists the same `MessagePart` objects)."""
    total = 0
    result: List[Message] = []
    for message in messages:
        new_parts = []
        changed = False
        for part in message.parts:
            if part.text:
                text, count = neutralize_system_tag(part.text)
                if count:
                    total += count
                    changed = True
                    part = part.model_copy(update={"text": text})
            new_parts.append(part)
        result.append(message.model_copy(update={"parts": new_parts}) if changed else message)
    return result, total


def scrub_value(value: Any, key: str) -> Tuple[Any, int]:
    """Scrub every string inside `value`, dict keys included (the first entry wins a collision)."""
    if isinstance(value, str):
        return scrub(value, key)
    if isinstance(value, dict):
        out: dict = {}
        total = 0
        for k, v in value.items():
            if isinstance(k, str):
                k, n = scrub(k, key)
                total += n
            v, n = scrub_value(v, key)
            total += n
            out.setdefault(k, v)
        return out, total
    if isinstance(value, (list, tuple)):
        items = []
        total = 0
        for item in value:
            item, n = scrub_value(item, key)
            items.append(item)
            total += n
        return type(value)(items), total
    if isinstance(value, RichContent):
        content_type, n1 = scrub_value(value.content_type, key)
        fallback, n2 = scrub_value(value.fallback_text, key)
        data, n3 = scrub_value(value.data, key)
        return dataclasses.replace(value, content_type=content_type, fallback_text=fallback, data=data), n1 + n2 + n3
    return value, 0


def scrub_reply(response: SmartResponse, summary: Optional[str], key: str) -> Tuple[SmartResponse, Optional[str], int]:
    """A new `SmartResponse` with text, structured data and link list scrubbed, and the scrubbed history summary."""
    text, n1 = scrub_value(response.text, key)
    structured, n2 = scrub_value(response.structured_data, key)
    links, n3 = scrub_value(response.link_list, key)
    new_summary, n4 = scrub_value(summary, key)
    return dataclasses.replace(response, text=text, structured_data=structured, link_list=links), new_summary, n1 + n2 + n3 + n4


def keyed_header(header: str, lines: List[str], key: str) -> str:
    """The header is the system's: keyed. The lines carry the owner's words: outside the tag, tag look-alikes neutralised."""
    head = wrap_system(header, key)
    if not lines:
        return head
    return head + "\n" + "\n".join(neutralize_system_tag(line)[0] for line in lines)
