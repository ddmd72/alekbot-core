"""The override rule for configuration: the effective value of a key is taken from the most specific level that sets
it — USER, else ACCOUNT, else SYSTEM. USER and ACCOUNT levels are sparse (`UserBotConfig` fields that store only what
they change; a removed key inherits again); the SYSTEM level declares every key, so a key it does not declare is not a
setting and is ignored at the other levels. Resolution takes no lock: the levels are read separately, and a write
between the reads is seen on the next resolve.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from .user import UserBotConfig


@dataclass(frozen=True)
class SystemConfig:
    """The SYSTEM level: the gated intents and their default availability (built from the agent declarations)."""
    intent_access: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_access", MappingProxyType(dict(self.intent_access)))


@dataclass(frozen=True)
class EffectiveConfig:
    """The resolved configuration of one user."""
    intent_access: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_access", MappingProxyType(dict(self.intent_access)))

    def is_intent_available(self, intent: str) -> bool:
        """An intent the SYSTEM level does not gate is always available."""
        return self.intent_access.get(intent, True)


def resolve(system: SystemConfig, account: Optional[UserBotConfig], user: Optional[UserBotConfig]) -> EffectiveConfig:
    """Per key of the SYSTEM level: the user's value, else the account's, else the SYSTEM default."""
    levels = [level.intent_access for level in (user, account) if level is not None]
    return EffectiveConfig(intent_access={
        intent: next((level[intent] for level in levels if intent in level), default)
        for intent, default in system.intent_access.items()
    })
