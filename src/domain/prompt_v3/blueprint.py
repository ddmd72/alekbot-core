"""
Blueprint - Defines prompt structure with tokenized classes.

Part of Prompt Design System v3 (RFC).
"""

import re
from dataclasses import dataclass
from typing import Dict

from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.token import Token


@dataclass(frozen=True)
class Blueprint:
    """Blueprint defines prompt structure with tokenized classes.

    A Blueprint is a template for assembling prompts:
    - Contains class definitions (BlueprintClass) with category and permission constraints
    - Groovy template with {{CLASS_NAME}} placeholders
    - Validates assignments via BlueprintClass.can_assign()

    Examples:
        >>> from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
        >>> from src.domain.prompt_v3.token import TokenId, TokenCategory
        >>>
        >>> blueprint = Blueprint(
        ...     id="smart_agent_v1",
        ...     classes={
        ...         "HUMOR_ENGINE": BlueprintClass(
        ...             allowed_token_categories={TokenCategory("humor_engine")},
        ...             overridable_by={OwnerType.USER},
        ...             default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
        ...         ),
        ...         "VOICE": BlueprintClass(
        ...             allowed_token_categories={TokenCategory("voice")},
        ...             overridable_by={OwnerType.ACCOUNT, OwnerType.USER},
        ...             default_token=TokenId("VOICE_CONVERSATIONAL")
        ...         ),
        ...     },
        ...     template='''class Alek {
        ...         properties {
        ...             {{HUMOR_ENGINE}}
        ...             {{VOICE}}
        ...         }
        ...     }'''
        ... )
        >>>
        >>> # Validate template references all classes
        >>> blueprint.validate()  # Passes
        >>>
        >>> # Check if token can be assigned to slot
        >>> humor_token = Token(...)
        >>> blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.USER)  # True
    """

    id: str  # e.g., "smart_agent_v1"
    classes: Dict[str, BlueprintClass]
    template: str  # Groovy template with {{CLASS_NAME}} placeholders

    def can_assign(self, class_name: str, token: Token, owner_type: OwnerType) -> bool:
        """Validate assignment via BlueprintClass.

        Args:
            class_name: Name of the class to assign to
            token: Token to assign
            owner_type: Owner type attempting the assignment

        Returns:
            True if assignment is allowed, False otherwise

        Examples:
            >>> blueprint = Blueprint(id="test", classes={...}, template="...")
            >>> humor_token = Token(
            ...     id=TokenId("HUMOR_OFF"),
            ...     category=TokenCategory("humor_engine"),
            ...     content="...",
            ...     metadata={}
            ... )
            >>>
            >>> # ✅ Valid class + category + permission
            >>> blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.USER)
            True
            >>>
            >>> # ❌ Class doesn't exist
            >>> blueprint.can_assign("INVALID_CLASS", humor_token, OwnerType.USER)
            False
        """
        if class_name not in self.classes:
            return False
        return self.classes[class_name].can_assign(token, owner_type)

    def validate(self) -> None:
        """Ensure all classes referenced in template exist.

        Raises:
            ValueError: If template references undefined classes

        Examples:
            >>> # ❌ Template references class not in self.classes
            >>> blueprint = Blueprint(
            ...     id="test",
            ...     classes={"HUMOR_ENGINE": BlueprintClass(...)},
            ...     template="{{HUMOR_ENGINE}} {{UNDEFINED_SLOT}}"
            ... )
            >>> blueprint.validate()  # Raises ValueError
            ValueError: Template references undefined classes: {'UNDEFINED_SLOT'}

            >>> # ✅ All template classes defined
            >>> blueprint = Blueprint(
            ...     id="test",
            ...     classes={"HUMOR_ENGINE": BlueprintClass(...)},
            ...     template="{{HUMOR_ENGINE}}"
            ... )
            >>> blueprint.validate()  # Passes
        """
        class_refs = set(re.findall(r'\{\{(\w+)\}\}', self.template))
        missing = class_refs - set(self.classes.keys())
        if missing:
            raise ValueError(f"Template references undefined classes: {missing}")

    def get_required_classes(self) -> set[str]:
        """Extract all class names referenced in template.

        Returns:
            Set of class names found in {{CLASS_NAME}} placeholders

        Examples:
            >>> blueprint = Blueprint(
            ...     id="test",
            ...     classes={...},
            ...     template="{{HUMOR_ENGINE}} some text {{VOICE}}"
            ... )
            >>> blueprint.get_required_classes()
            {'HUMOR_ENGINE', 'VOICE'}
        """
        return set(re.findall(r'\{\{(\w+)\}\}', self.template))

    def __hash__(self):
        """Enable Blueprint to be used in sets/dicts."""
        # classes is a dict, need to convert to frozenset of items for hashing
        return hash((
            self.id,
            frozenset(self.classes.items()),
            self.template
        ))
