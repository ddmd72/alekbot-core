"""
ProfileSlot - Unified slot declaration for agent profiles.

Represents which token classes/categories/tokens are allowed and whether
they are non-overridable for a given agent profile.
"""

from dataclasses import dataclass
from enum import Enum


class ProfileSlotType(str, Enum):
    """Type of slot entry in agent profile."""
    CLASS = "class"       # Blueprint section (properties, policies, protocols, ...)
    CATEGORY = "category" # Token category (humor_engine, voice, ...)
    TOKEN = "token"       # specific token id
    SLOT = "slot"         # Exclude slot (non_overridable=true)


@dataclass(frozen=True)
class ProfileSlot:
    """Unified slot declaration for agent profiles."""
    type: ProfileSlotType
    value: str
    non_overridable: bool = False

    @staticmethod
    def from_dict(data: dict) -> "ProfileSlot":
        return ProfileSlot(
            type=ProfileSlotType(data["type"]),
            value=data["value"],
            non_overridable=bool(data.get("non_overridable", False))
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "value": self.value,
            "non_overridable": self.non_overridable,
        }