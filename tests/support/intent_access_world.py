"""A real coordinator over the registry of every declaration, with per-user intent availability (CONFIG_OVERRIDE_HIERARCHY_RFC §3).

`coordinator_denying({"usr-1": {"search_memory"}})` resolves the SYSTEM defaults of `ALL_DESCRIPTORS` with those intents
turned off on that user's profile; any other user has no overrides. `delegate_intents(tool)` reads the intents a built
delegate tool declaration lists, one `- <intent>: <description>` line each.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from src.domain.config_resolution import EffectiveConfig, SystemConfig, resolve
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry, system_intent_defaults


def coordinator_denying(denied: Mapping[str, Iterable[str]]) -> AgentCoordinator:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    system = SystemConfig(intent_access=system_intent_defaults(ALL_DESCRIPTORS))

    async def effective_config(user_id: str) -> EffectiveConfig:
        user = UserBotConfig(intent_access={intent: False for intent in denied.get(user_id, ())})
        return resolve(system, None, user)

    return AgentCoordinator(registry=registry, effective_config=effective_config)


def delegate_tool(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    (tool,) = [t for t in tools if t["name"] == "delegate_to_specialist"]
    return tool


def delegate_intents(tool: Dict[str, Any]) -> List[str]:
    """The intent names the tool's description lists, in order."""
    return [line[2:].split(":", 1)[0] for line in tool["description"].splitlines() if line.startswith("- ")]
