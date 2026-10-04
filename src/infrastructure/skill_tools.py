"""Smart's use_skill tool — served locally by the DelegationEngine, never via the coordinator."""

from typing import Any, Dict, Sequence, Set

from ..domain.llm import ToolCall
from ..domain.skill import SKILL_CONTEXT_KEY, USE_SKILL_TOOL, Skill, body_marker
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
