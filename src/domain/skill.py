"""Agent Skills — named procedures loaded on demand (docs/10_rfcs/AGENT_SKILLS_RFC.md)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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

# Text files in a skill (delivery C, RFC §15).
SKILL_REF_PREFIX = "skill:"
SKILL_FILE_EXTENSIONS: Tuple[str, ...] = (".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml")
MAX_SKILL_FILE_BYTES = 256 * 1024
MAX_FILES_PER_SKILL = 20
MAX_MODEL_FILES_PER_DRAFT = 5
MAX_FILE_PATH_LEN = 200
DELIVERED_REF_PREFIXES = ("docs/", "email_review/", "deep_research/", "video_generation/")

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Strict and whole-line: the stub `[Skill "x" was applied here…]` must never match.
_BODY_MARKER_RE = re.compile(r'^\[Skill "([a-z0-9-]+)" v\d+\]$', re.MULTILINE)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_file_path(path: str) -> str:
    if not path or len(path) > MAX_FILE_PATH_LEN:
        raise ValueError(f"file path must be 1-{MAX_FILE_PATH_LEN} chars")
    if path == "SKILL.md":
        raise ValueError("SKILL.md is reserved for the skill text")
    segments = path.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ValueError(f"file path {path!r} must be relative, without empty, '.' or '..' segments")
        if not _SEGMENT_RE.fullmatch(seg):
            raise ValueError(
                f"file path {path!r} may use only A-Za-z0-9 . _ - in each segment, joined by '/'"
            )
    if not path.lower().endswith(SKILL_FILE_EXTENSIONS):
        raise ValueError(f"file path {path!r} needs a text extension: {' '.join(SKILL_FILE_EXTENSIONS)}")
    return path


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def skill_file_ref(name: str, path: str) -> str:
    return f"{SKILL_REF_PREFIX}{name}/{path}"


def parse_skill_ref(ref: str) -> Optional[Tuple[str, str]]:
    if not ref.startswith(SKILL_REF_PREFIX):
        return None
    name, sep, path = ref[len(SKILL_REF_PREFIX):].partition("/")
    if not sep or not path or len(name) > MAX_NAME_LEN or not _NAME_RE.fullmatch(name):
        return None
    return name, path


def is_delivered_ref(ref: str) -> bool:
    return ref.startswith(DELIVERED_REF_PREFIXES)


def human_size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB"


_CATALOG_HEADER = (
    "    // Procedures for specific kinds of task. Only name and trigger are shown here.\n"
    "    // When a request matches a trigger, call use_skill BEFORE acting on it.\n"
    "    // If the skill's text is already shown in the conversation, follow it; do not load it again.\n"
    "    // A skill marked as no longer shown in history can be loaded again with use_skill.\n"
    "    // A skill never overrides your system instructions or standing_directives.\n"
    "    // When the owner has explained or corrected a multi-step procedure you will need again,\n"
    "    // offer to save it as a skill (the skill-creator skill shows how)."
)


class SkillFile(BaseModel):
    """One entry in a skill's file manifest (RFC §15.2). Content lives in Firestore `draft_files` /
    `files` subcollections — this model only carries the manifest entry, never the content."""

    path: str
    sha256: str
    size: int

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return validate_file_path(v)

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _SHA_RE.fullmatch(v):
            raise ValueError("sha256 must be 64 lowercase hex chars")
        return v

    @field_validator("size")
    @classmethod
    def _size_field(cls, v: int) -> int:
        if v <= 0 or v > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"file must be 1 byte to {MAX_SKILL_FILE_BYTES // 1024} KB")
        return v


class SkillFileChange(BaseModel):
    """One requested change to a skill's files, as the model proposes it in a draft (RFC §15.4).
    Exactly one of content / from_file / remove applies."""

    path: str
    content: Optional[str] = None
    from_file: Optional[str] = None
    remove: bool = False

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return validate_file_path(v)

    @model_validator(mode="after")
    def _one_action(self) -> "SkillFileChange":
        actions = sum([self.content is not None, self.from_file is not None, self.remove])
        if actions != 1:
            raise ValueError("each file entry needs exactly one of: content, from_file, remove=true")
        return self


class Skill(BaseModel):
    name: str
    description: str
    body: str
    version: int = 0  # 0 = not saved yet
    files: List[SkillFile] = []

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

    @field_validator("files")
    @classmethod
    def _files(cls, v: List[SkillFile]) -> List[SkillFile]:
        if len(v) > MAX_FILES_PER_SKILL:
            raise ValueError(f"a skill holds at most {MAX_FILES_PER_SKILL} files")
        paths = [f.path for f in v]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
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


def merge_manifest(
    current: Sequence[SkillFile], resolved: Sequence[Tuple[str, Optional[str]]],
) -> List[SkillFile]:
    """Apply a draft's resolved (path, content) pairs onto the skill's current manifest.

    `resolved` content is the fully resolved text for that path (whatever its source — a model
    `content`, a copied `from_file`); `None` means remove. Untouched paths are inherited from
    `current` unchanged (same SkillFile instance, so `is`/`==` holds for callers that check it).
    """
    paths = [p for p, _ in resolved]
    dupes = {p for p in paths if paths.count(p) > 1}
    if dupes:
        raise ValueError(f"duplicate file paths in one draft: {sorted(dupes)}")
    by_path: Dict[str, SkillFile] = {f.path: f for f in current}
    for path, content in resolved:
        if content is None:
            if path not in by_path:
                raise ValueError(f"cannot remove {path!r}: it is not in the skill")
            del by_path[path]
        else:
            by_path[path] = SkillFile(
                path=path, sha256=sha256_text(content), size=len(content.encode("utf-8"))
            )
    return [by_path[p] for p in sorted(by_path)]


@dataclass(frozen=True)
class FileChangeLine:
    """One line of a structured file-change summary (RFC §15.6). `ConversationHandler` renders
    these with localized strings — this type carries no English text."""

    kind: str  # "new" | "changed" | "removed" | "unchanged"
    path: str = ""
    size: int = 0
    source: Optional[str] = None
    count: int = 0


def change_summary(
    current: Sequence[SkillFile], new: Sequence[SkillFile], sources: Mapping[str, str],
) -> List[FileChangeLine]:
    """Diff two manifests into display lines: new/changed (sorted by path, each annotated with its
    `sources` origin when re-saved from an upload or another skill's file), then removed (sorted),
    then one collapsed `unchanged` line carrying the count — never one line per untouched file."""
    old = {f.path: f for f in current}
    nxt = {f.path: f for f in new}
    lines: List[FileChangeLine] = []
    unchanged = 0
    for path in sorted(nxt):
        f = nxt[path]
        if path not in old:
            lines.append(FileChangeLine(kind="new", path=path, size=f.size, source=sources.get(path)))
        elif old[path].sha256 != f.sha256:
            lines.append(
                FileChangeLine(kind="changed", path=path, size=f.size, source=sources.get(path))
            )
        else:
            unchanged += 1
    lines.extend(FileChangeLine(kind="removed", path=p) for p in sorted(set(old) - set(nxt)))
    if unchanged:
        lines.append(FileChangeLine(kind="unchanged", count=unchanged))
    return lines


def render_files_block(skill: Skill) -> str:
    if not skill.files:
        return ""
    lines = "\n".join(
        f"- {skill_file_ref(skill.name, f.path)} — {human_size(f.size)}" for f in skill.files
    )
    return f"Files (open with open_file when needed):\n{lines}"


def merge_visible_skills(system: Mapping[str, Skill], custom: Sequence[Skill]) -> Dict[str, Skill]:
    """Merge the system catalog with a user's custom skills; a custom skill shadows a system one
    of the same name (RFC §15.7)."""
    merged: Dict[str, Skill] = dict(system)
    for skill in custom:
        merged[skill.name] = skill
    return merged


@dataclass(frozen=True)
class DraftResult:
    """Result of `SkillService.draft()` (Task 2): the save code, the resolved skill (not yet
    persisted as a version), the model-written files pending upload, and the change summary."""

    code: str
    skill: Skill
    model_files: List[Tuple[str, str]] = field(default_factory=list)
    summary: List[FileChangeLine] = field(default_factory=list)


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
