"""
BlueprintClass - Defines allowed tokens and override permissions for a class.

Part of Prompt Design System v3 (RFC).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Set

from src.domain.prompt_v3.token import Token, TokenCategory, TokenId


class OwnerType(Enum):
    """Owner type for slot assignments (4-level priority hierarchy)."""
    SYSTEM = "system"    # Lowest priority (global defaults)
    AGENT = "agent"      # Agent-specific defaults
    ACCOUNT = "account"  # Account-level customization
    USER = "user"        # Highest priority (user-specific overrides)


@dataclass(frozen=True)
class BlueprintClass:
    """Defines allowed tokens and override permissions for a blueprint class.

    BlueprintClass enforces structural constraints at domain level:
    - Which token categories can be assigned to this class
    - Which owner types can override this class
    - Default token if no override provided

    Examples:
        >>> # Humor engine class: overridable by USER only
        >>> humor_class = BlueprintClass(
        ...     allowed_token_categories={TokenCategory("humor_engine")},
        ...     overridable_by={OwnerType.USER},
        ...     default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
        ... )
        >>>
        >>> # Voice class: overridable by ACCOUNT and USER
        >>> voice_class = BlueprintClass(
        ...     allowed_token_categories={TokenCategory("voice"), TokenCategory("tone")},
        ...     overridable_by={OwnerType.ACCOUNT, OwnerType.USER},
        ...     default_token=TokenId("VOICE_CONVERSATIONAL")
        ... )
    """

    allowed_token_categories: Set[TokenCategory]
    overridable_by: Set[OwnerType]
    default_token: TokenId

    def can_assign(self, token: Token, owner_type: OwnerType) -> bool:
        """Domain-level validation: category + permission check.

        Args:
            token: Token to assign to this class
            owner_type: Owner type attempting the assignment

        Returns:
            True if assignment is allowed, False otherwise

        Examples:
            >>> class_schema = BlueprintClass(
            ...     allowed_token_categories={TokenCategory("humor_engine")},
            ...     overridable_by={OwnerType.USER},
            ...     default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
            ... )
            >>>
            >>> humor_token = Token(
            ...     id=TokenId("HUMOR_PRESET_OFF"),
            ...     category=TokenCategory("humor_engine"),
            ...     content="...",
            ...     metadata={}
            ... )
            >>>
            >>> # ✅ USER can override humor class with humor_engine token
            >>> assert class_schema.can_assign(humor_token, OwnerType.USER) == True
            >>>
            >>> # ❌ SYSTEM cannot override (not in overridable_by)
            >>> assert class_schema.can_assign(humor_token, OwnerType.SYSTEM) == False
            >>>
            >>> voice_token = Token(
            ...     id=TokenId("VOICE_FORMAL"),
            ...     category=TokenCategory("voice"),
            ...     content="...",
            ...     metadata={}
            ... )
            >>>
            >>> # ❌ Wrong category (voice token cannot go into humor class)
            >>> assert class_schema.can_assign(voice_token, OwnerType.USER) == False
        """
        return (
            token.category in self.allowed_token_categories
            and owner_type in self.overridable_by
        )

    def __hash__(self):
        """Enable BlueprintClass to be used in sets/dicts."""
        return hash((
            frozenset(self.allowed_token_categories),
            frozenset(self.overridable_by),
            self.default_token
        ))
