"""Smart's use_skill / draft_skill tools — served locally by the DelegationEngine, never via the coordinator."""

from typing import Any, Awaitable, Callable, Dict, Sequence, Set

from pydantic import ValidationError

from ..domain.agent import DeliveryItem
from ..domain.exceptions import SkillCapExceeded, SkillNameReserved, SkillRejected
from ..domain.llm import ToolCall
from ..domain.skill import (
    DRAFT_SKILL_TOOL,
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    SKILL_CONTEXT_KEY,
    SKILL_PREVIEW_DELIVERY,
    USE_SKILL_TOOL,
    Skill,
    body_marker,
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
        return ToolResult(
            name=tool_call.name,
            result_str=f"{body_marker(skill.name, skill.version)}\n{skill.body}",
            history_context={SKILL_CONTEXT_KEY: {"name": skill.name, "version": skill.version, "body": skill.body}},
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
            },
            "required": ["name", "description", "body"],
        },
    }


def make_draft_skill_handler(draft: Callable[[Skill], Awaitable[str]]) -> LocalToolHandler:
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

        try:
            code = await draft(skill)
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
                        "name": skill.name,
                        "skill_md": render_skill_md(skill),
                        "command": save_command(code),
                    },
                )
            ],
        )

    return handle
