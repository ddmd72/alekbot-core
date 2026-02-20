"""
SlotExclusion Domain Model - Phase 5-1, Day 3.1
================================================
Represents slot exclusions with type discriminator for hierarchical structure.

Two types of exclusions:
1. SLOT: Exclude specific slot by name (e.g., "HUMOR_ENGINE")
    2. CATEGORY: Exclude all classes in category (e.g., "humor_engine")

Part of Prompt Design System v3 (RFC).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Union


class ExclusionType(str, Enum):
    """Type discriminator for slot exclusions."""
    SLOT = "slot"
    CATEGORY = "category"


@dataclass(frozen=True)
class SlotExclusion:
    """
    Represents a slot exclusion with type discriminator.

    Examples:
        >>> # Exclude specific slot
        >>> SlotExclusion(type=ExclusionType.SLOT, value="HUMOR_ENGINE")
        SlotExclusion(type='slot', value='HUMOR_ENGINE')

        >>> # Exclude all classes in category
        >>> SlotExclusion(type=ExclusionType.CATEGORY, value="humor_engine")
        SlotExclusion(type='category', value='humor_engine')
    """
    type: ExclusionType
    value: str

    def __str__(self) -> str:
        """String representation for logging."""
        return f"{self.type.value}:{self.value}"

    @staticmethod
    def from_slot_name(slot_name: str) -> "SlotExclusion":
        """Create slot-level exclusion from slot name.

        Args:
            slot_name: Slot name to exclude (e.g., "HUMOR_ENGINE")

        Returns:
            SlotExclusion with type=SLOT
        """
        return SlotExclusion(type=ExclusionType.SLOT, value=slot_name)

    @staticmethod
    def from_category(category: str) -> "SlotExclusion":
        """Create category-level exclusion from category name.

        Args:
            category: Category to exclude (e.g., "humor_engine")

        Returns:
            SlotExclusion with type=CATEGORY
        """
        return SlotExclusion(type=ExclusionType.CATEGORY, value=category)

    @staticmethod
    def from_dict(data: dict) -> "SlotExclusion":
        """Create SlotExclusion from dictionary.

        Args:
            data: Dictionary with 'type' and 'value' keys

        Returns:
            SlotExclusion instance

        Examples:
            >>> SlotExclusion.from_dict({"type": "slot", "value": "HUMOR_ENGINE"})
            SlotExclusion(type='slot', value='HUMOR_ENGINE')
        """
        return SlotExclusion(
            type=ExclusionType(data["type"]),
            value=data["value"]
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary with 'type' and 'value' keys
        """
        return {
            "type": self.type.value,
            "value": self.value
        }

    @staticmethod
    def from_string(s: str) -> "SlotExclusion":
        """Create SlotExclusion from legacy string format.

        For backward compatibility with existing flat string lists.

        Args:
            s: Slot name string (e.g., "HUMOR_ENGINE")

        Returns:
            SlotExclusion with type=SLOT
        """
        return SlotExclusion.from_slot_name(s)


# Type alias for convenience
ExclusionList = list[Union[SlotExclusion, str]]
