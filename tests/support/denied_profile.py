"""A user whose profile turns intents off, injected into a real `AgentCoordinator` (the shape `ConfigurationService.effective_for`
returns: the SYSTEM level resolved over the user's own `intent_access`).

`coordinator_denying({"usr-1": {"search_memory"}})` is a real coordinator over the registry of every declaration where those
intents are off for that user only; `delegate_intents(tool)` reads the intents a built delegate tool declaration lists."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping

from src.domain.config_resolution import EffectiveConfig, SystemConfig, resolve
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry, system_intent_defaults

EffectiveFor = Callable[[str], Awaitable[EffectiveConfig]]


def with_user_entry(system_gated: dict, user_entry: dict) -> EffectiveFor:
    """`effective_for` over a SYSTEM level that gates `system_gated` and a user level holding `user_entry`."""
    async def effective_for(user_id: str) -> EffectiveConfig:
        return resolve(SystemConfig(intent_access=system_gated), None, UserBotConfig(intent_access=user_entry))
    return effective_for


def denying(*intents: str) -> EffectiveFor:
    """The user's profile turns `intents` off (they are gated, on by default)."""
    return with_user_entry({i: True for i in intents}, {i: False for i in intents})


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
