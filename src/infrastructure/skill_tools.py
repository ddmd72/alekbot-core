"""Smart's use_skill / draft_skill tools — served locally by the DelegationEngine, never via the coordinator."""

from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, List, Sequence, Set

from pydantic import ValidationError

from ..domain.agent import DeliveryItem
from ..domain.exceptions import SkillCapExceeded, SkillNameReserved, SkillRejected
from ..domain.llm import ToolCall
from ..domain.skill import (
    DRAFT_SKILL_TOOL,
    MAX_DESCRIPTION_LEN,
    MAX_FILE_PATH_LEN,
    MAX_MODEL_FILES_PER_DRAFT,
    MAX_NAME_LEN,
    MAX_SKILL_FILE_BYTES,
    SKILL_CONTEXT_KEY,
    SKILL_FILE_EXTENSIONS,
    SKILL_PREVIEW_DELIVERY,
    USE_SKILL_TOOL,
    DraftResult,
    Skill,
    SkillFileChange,
    body_marker,
    render_files_block,
    render_skill_md,
    save_command,
)
from .delegation_engine import LocalToolHandler, ToolResult
from ..utils.logger import logger


def build_use_skill_tool_declaration() -> Dict[str, Any]:
    return {
        "name": USE_SKILL_TOOL,
        "description": (
            "Load the full text of a skill listed in available_skills. "
            "Call it BEFORE acting on a request that matches the skill's trigger, then follow the text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name exactly as listed in available_skills."},
            },
            "required": ["name"],
        },
    }


def make_use_skill_handler(skills: Sequence[Skill], visible: Set[str]) -> LocalToolHandler:
    """Build per execution: `loaded_now` must never outlive one transcript (provider rotation restarts it)."""
    by_name = {s.name: s for s in skills}
    loaded_now: Set[str] = set()

    async def handle(tool_call: ToolCall) -> ToolResult:
        name = str((tool_call.args or {}).get("name", "")).strip()
        skill = by_name.get(name)
        if skill is None:
            logger.info("🧩 [use_skill] miss name=%s", name)
            available = ", ".join(sorted(by_name)) or "none"
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: No skill named '{name}'. Available skills: {available}.",
                failed=True,
            )
        if name in visible or name in loaded_now:
            logger.info("🧩 [use_skill] already-visible name=%s", name)
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: The text of skill '{name}' is already in your context above; follow it.",
            )
        loaded_now.add(name)
        logger.info("🧩 [use_skill] loaded name=%s v%s", name, skill.version)
        text = skill.body if not skill.files else f"{skill.body}\n\n{render_files_block(skill)}"
        return ToolResult(
            name=tool_call.name,
            result_str=f"{body_marker(skill.name, skill.version)}\n{text}",
            history_context={SKILL_CONTEXT_KEY: {"name": skill.name, "version": skill.version, "body": text}},
        )

    return handle


def build_draft_skill_tool_declaration() -> Dict[str, Any]:
    return {
        "name": DRAFT_SKILL_TOOL,
        "description": (
            "Propose a new skill for the owner to save. Delivers a preview (as a file) plus a save "
            "command the owner can paste to confirm it — the skill is NOT saved by this call. "
            "Use when the owner asks you to remember a procedure, or you have just learned one worth saving."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        f"Kebab-case identifier [a-z0-9-], at most {MAX_NAME_LEN} chars "
                        "(e.g. 'flight-status')."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One non-empty line, at most "
                        f"{MAX_DESCRIPTION_LEN} chars, stating when this skill applies — "
                        "shown in the catalog to decide whether to load it."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "The full procedure text to follow when the skill is loaded. Must not be empty.",
                },
                "files": {
                    "type": "array",
                    "description": (
                        "Optional changes to the skill's text files. Allowed extensions: "
                        f"{' '.join(SKILL_FILE_EXTENSIONS)}. At most {MAX_SKILL_FILE_BYTES // 1024} KB per "
                        f"file, and at most {MAX_MODEL_FILES_PER_DRAFT} {{path, content}} entries written "
                        "directly in this call per draft — use from_file (or several drafts) for more. "
                        "Only the changes listed here are applied; every file not listed carries over "
                        "unchanged from the skill's current version."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "Relative path within the skill, e.g. 'references/notes.md'. "
                                    f"At most {MAX_FILE_PATH_LEN} chars; 'SKILL.md' is reserved."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "The file's new full text content. Exactly one of content, "
                                    "from_file, remove must be set per entry."
                                ),
                            },
                            "from_file": {
                                "type": "string",
                                "description": (
                                    "Copy content from elsewhere instead of writing it here: either a "
                                    "bare uploaded filename (from the file's label in this conversation), "
                                    "or a 'skill:<name>/<path>' reference to one of the owner's own "
                                    "custom skills' files. Exactly one of content, from_file, remove "
                                    "must be set per entry."
                                ),
                            },
                            "remove": {
                                "type": "boolean",
                                "description": (
                                    "Set true to delete this path from the skill. Exactly one of "
                                    "content, from_file, remove must be set per entry."
                                ),
                            },
                        },
                    },
                },
            },
            "required": ["name", "description", "body"],
        },
    }


def make_draft_skill_handler(
    draft: Callable[[Skill, List[SkillFileChange]], Awaitable[DraftResult]],
) -> LocalToolHandler:
    """Build per execution: the closure binds the requesting user's id via `draft`."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        name = str(args.get("name", ""))
        description = str(args.get("description", ""))
        body = str(args.get("body", ""))
        try:
            skill = Skill(name=name, description=description, body=body)
        except ValidationError as e:
            reason = "; ".join(err["msg"] for err in e.errors()) or str(e)
            logger.info("🧩 [draft_skill] validation rejected name=%s: %s", name, reason)
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: draft rejected — {reason}. Fix it and call draft_skill again.",
                failed=True,
            )

        raw_files = args.get("files")
        if raw_files is None:
            changes: List[SkillFileChange] = []
        elif not isinstance(raw_files, list):
            logger.info("🧩 [draft_skill] files not a list name=%s", name)
            return ToolResult(
                name=tool_call.name,
                result_str=(
                    "SYSTEM: draft rejected — files must be a list of file-change objects. "
                    "Fix it and call draft_skill again."
                ),
                failed=True,
            )
        else:
            changes = []
            # One entry at a time, so the error names the entry the model must fix.
            for i, entry in enumerate(raw_files):
                path = entry.get("path") if isinstance(entry, dict) else None
                label = f"files[{i}] ({path})" if path else f"files[{i}]"
                if not isinstance(entry, dict):
                    reason = "must be an object with path and one of content, from_file, remove"
                else:
                    try:
                        changes.append(SkillFileChange.model_validate(entry))
                        continue
                    except ValidationError as e:
                        reason = "; ".join(err["msg"] for err in e.errors()) or str(e)
                logger.info("🧩 [draft_skill] files entry rejected name=%s: %s: %s", name, label, reason)
                return ToolResult(
                    name=tool_call.name,
                    result_str=(
                        f"SYSTEM: draft rejected — {label}: {reason}. Fix it and call draft_skill again."
                    ),
                    failed=True,
                )

        try:
            res = await draft(skill, changes)
        except (SkillRejected, SkillNameReserved, SkillCapExceeded) as e:
            logger.info("🧩 [draft_skill] draft rejected name=%s: %s", name, e)
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: draft rejected — {e}. Fix it and call draft_skill again.",
                failed=True,
            )
        except Exception as e:
            logger.error("❌ [draft_skill] draft could not be stored for name=%s: %s", name, e)
            return ToolResult(
                name=tool_call.name,
                result_str="SYSTEM: draft could not be stored.",
                failed=True,
            )

        logger.info("📝 [draft_skill] drafted name=%s", name)
        return ToolResult(
            name=tool_call.name,
            result_str=(
                "SYSTEM: Preview delivered to the owner as a file with a save command. Tell the "
                "owner in one or two sentences what the skill does and that pasting the command "
                "saves it. It is NOT saved yet."
            ),
            delivery_items=[
                DeliveryItem(
                    type=SKILL_PREVIEW_DELIVERY,
                    data={
                        "name": res.skill.name,
                        "skill_md": render_skill_md(res.skill),
                        "command": save_command(res.code),
                        "files": [{"path": p, "content": c} for p, c in res.model_files],
                        "summary": [asdict(line) for line in res.summary],
                    },
                )
            ],
        )

    return handle
