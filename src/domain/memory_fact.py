"""Memory facts the owner confirms (docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, field_validator

PROPOSE_MEMORY_FACTS_TOOL = "propose_memory_facts"
MEMORY_PREVIEW_DELIVERY = "memory_preview"

MAX_FACTS_PER_DRAFT = 20
MAX_FACT_CHARS = 300
MAX_SOURCE_CHARS = 150


def _one_line(v: str, limit: int, what: str) -> str:
    v = v.strip()
    if not v or len(v.splitlines()) > 1 or len(v) > limit:
        raise ValueError(f"{what} must be one non-empty line of at most {limit} characters")
    return v


class ProposedFact(BaseModel):
    text: str
    source: str

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return _one_line(v, MAX_FACT_CHARS, "text")

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        return _one_line(v, MAX_SOURCE_CHARS, "source")


class MemoryFactDraft(BaseModel):
    facts: List[ProposedFact]
    # The session the facts were proposed in; `$memory save` appends them there, wherever it is pasted.
    origin_session_id: Optional[str] = None

    @field_validator("facts")
    @classmethod
    def _facts(cls, v: List[ProposedFact]) -> List[ProposedFact]:
        if not v:
            raise ValueError("propose at least one fact")
        if len(v) > MAX_FACTS_PER_DRAFT:
            raise ValueError(f"at most {MAX_FACTS_PER_DRAFT} facts per proposal — split the rest into another one")
        return v


def save_command(code: str) -> str:
    return f"$memory save {code}"


def render_preview_file(draft: MemoryFactDraft) -> str:
    """The numbered facts as plain text, posted as a file: chat adapters truncate long messages
    silently and the owner must never confirm a fact they were not shown."""
    return "\n".join(
        f"{i}. {f.text}\n   source: {f.source}\n" for i, f in enumerate(draft.facts, start=1)
    )


def render_confirmed_note(draft: MemoryFactDraft) -> str:
    """The user part of the history pair `$memory save` appends.

    The facts must first appear in a user message the owner caused: the consolidator discards
    facts an ASSISTANT stated and the user only confirmed (RFC §2, echo trap). Per-fact sources
    stay out of the lines: a file name beside a fact risks the consolidator's file-operations
    exclusion.
    """
    sources = ", ".join(dict.fromkeys(f.source for f in draft.facts))
    lines = "\n".join(f"- {f.text}" for f in draft.facts)
    return (
        "[System: the owner reviewed and confirmed these facts about their life and the people in it, "
        f"read from: {sources}. Background consolidation adds them to memory; "
        "search_memory cannot find them before that.]\n" + lines
    )


def confirmed_reply(count: int) -> str:
    """The model part of the history pair."""
    return f"Submitted {count} confirmed facts to memory."
