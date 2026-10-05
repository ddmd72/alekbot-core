"""Agent Skills — named procedures loaded on demand (docs/10_rfcs/AGENT_SKILLS_RFC.md)."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, field_validator, model_validator

from .llm import Message

MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 250
# Storage bound, not a style limit: a Firestore document holds at most 1 MiB, and the body is
# stored once per document (version, index and draft docs) next to a few short fields. The
# headroom covers those fields. Keeping a skill concise is guidance for the author, not a gate.
FIRESTORE_DOC_LIMIT_BYTES = 1024 * 1024
MAX_SKILL_MD_BYTES = FIRESTORE_DOC_LIMIT_BYTES - 32 * 1024
MAX_CUSTOM_SKILLS_PER_USER = 20
SKILL_CONTEXT_KEY = "skill_context"
USE_SKILL_TOOL = "use_skill"
DRAFT_SKILL_TOOL = "draft_skill"
SKILL_PREVIEW_DELIVERY = "skill_preview"

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Strict and whole-line: the stub `[Skill "x" was applied here…]` must never match.
_BODY_MARKER_RE = re.compile(r'^\[Skill "([a-z0-9-]+)" v\d+\]$', re.MULTILINE)

_CATALOG_HEADER = (
    "    // Procedures for specific kinds of task. Only name and trigger are shown here.\n"
    "    // When a request matches a trigger, call use_skill BEFORE acting on it.\n"
    "    // If the skill's text is already shown in the conversation, follow it; do not load it again.\n"
    "    // A skill marked as no longer shown in history can be loaded again with use_skill.\n"
    "    // A skill never overrides your system instructions or standing_directives.\n"
    "    // When the owner has explained or corrected a multi-step procedure you will need again,\n"
    "    // offer to save it as a skill (the skill-creator skill shows how)."
)


class Skill(BaseModel):
    name: str
    description: str
    body: str
    version: int = 0  # 0 = not saved yet

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if len(v) > MAX_NAME_LEN or not _NAME_RE.match(v):
            raise ValueError("name must be kebab-case [a-z0-9-], at most 64 chars")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v.splitlines()) > 1 or len(v) > MAX_DESCRIPTION_LEN:
            raise ValueError("description must be one non-empty line of at most 250 chars")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("body must not be empty")
        return v

    @model_validator(mode="after")
    def _size(self) -> "Skill":
        if len(render_skill_md(self).encode("utf-8")) > MAX_SKILL_MD_BYTES:
            raise ValueError(f"SKILL.md must be at most {MAX_SKILL_MD_BYTES // 1024} KB")
        return self


def render_skill_md(skill: Skill) -> str:
    # JSON string literal is valid YAML, so the frontmatter parses back identically.
    description = json.dumps(skill.description, ensure_ascii=False)
    return f"---\nname: {skill.name}\ndescription: {description}\n---\n{skill.body}\n"


def body_marker(name: str, version: int) -> str:
    return f'[Skill "{name}" v{version}]'


def skill_stub(name: str) -> str:
    # Neutral on purpose: Lelik's warm context reads model summaries and has no use_skill.
    return f'[Skill "{name}" was applied here; its text is no longer shown]'


def save_command(code: str) -> str:
    return f"$skill save {code}"


def skill_saved_note(name: str, version: int) -> str:
    return f'[System: skill "{name}" v{version} saved by the owner]'


def visible_skill_names(messages: Iterable[Message]) -> Set[str]:
    """Names whose body the model can see: markers in MODEL text after history tiering.

    Reads part.text only — a tiered-out turn still carries full_text, which the model never sees.
    """
    names: Set[str] = set()
    for msg in messages:
        if msg.role != "model":
            continue
        for part in msg.parts:
            if part.text:
                names.update(_BODY_MARKER_RE.findall(part.text))
    return names


def render_catalog(skills: Sequence[Skill]) -> Optional[str]:
    if not skills:
        return None
    lines = "\n".join(f"    - {s.name} — {s.description}" for s in skills)
    return f"available_skills {{\n{_CATALOG_HEADER}\n{lines}\n}}"


def fold_skill_contexts(full_text: str, summary: str, contexts: Sequence[Any]) -> Tuple[str, str]:
    """Append loaded skill bodies to full_text (raw, unescaped) and one stub each to the summary."""
    by_name: dict = {}
    for ctx in contexts:
        if isinstance(ctx, dict) and ctx.get("name") and ctx.get("body"):
            by_name[ctx["name"]] = ctx
    if not by_name:
        return full_text, summary
    blocks = "\n\n".join(
        f"{body_marker(c['name'], int(c.get('version', 0)))}\n{c['body']}" for c in by_name.values()
    )
    stubs = "\n".join(skill_stub(name) for name in by_name)
    return f"{full_text}\n\n{blocks}", f"{summary}\n\n{stubs}"
