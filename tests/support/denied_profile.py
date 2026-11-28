"""A user whose profile turns intents off, injected into a real `AgentCoordinator` (the shape `ConfigurationService.effective_for`
returns: the SYSTEM level resolved over the user's own `intent_access`)."""
from __future__ import annotations

from typing import Awaitable, Callable

from src.domain.config_resolution import EffectiveConfig, SystemConfig, resolve
from src.domain.user import UserBotConfig

EffectiveFor = Callable[[str], Awaitable[EffectiveConfig]]


def with_user_entry(system_gated: dict, user_entry: dict) -> EffectiveFor:
    """`effective_for` over a SYSTEM level that gates `system_gated` and a user level holding `user_entry`."""
    async def effective_for(user_id: str) -> EffectiveConfig:
        return resolve(SystemConfig(intent_access=system_gated), None, UserBotConfig(intent_access=user_entry))
    return effective_for


def denying(*intents: str) -> EffectiveFor:
    """The user's profile turns `intents` off (they are gated, on by default)."""
    return with_user_entry({i: True for i in intents}, {i: False for i in intents})
