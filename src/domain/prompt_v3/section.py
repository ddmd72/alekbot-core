"""
SectionType - Classification of prompt sections by handling strategy.

Part of Prompt Design System v3 (RFC).
"""

from enum import Enum


class SectionType(Enum):
    """How different parts of prompt are handled.

    The Prompt Design System v3 distinguishes three types of content:

    1. **TOKENIZED**: Resolved from token library via classes
       - Examples: archetype, humor_engine, voice, cognitive_process
       - User selects from whitelisted tokens
       - Controlled by BlueprintClass (category + permission constraints)
       - Highest security (no raw text injection)

    2. **STATIC**: Stored with blueprint/agent as read-only content
       - Examples: policies, few_shot_examples, domain_knowledge
       - Managed by SYSTEM/AGENT only
       - Cannot be overridden by ACCOUNT/USER
       - Medium security (admin-controlled)

    3. **RUNTIME**: Injected at request time, validated by SecurityPort
       - Examples: biographical_context, conversation_history, semantic_context
       - Dynamic per-request data
       - Validated by SecurityPort (multi-layer defense)
       - Requires runtime validation (INPUT + OUTPUT + RAG)

    Security Architecture:
        TOKENIZED → No validation needed (pre-approved tokens)
        STATIC    → No validation needed (admin-controlled)
        RUNTIME   → SecurityPort validation REQUIRED

    Examples:
        >>> # TOKENIZED section: User selects from tokens
        >>> if section_type == SectionType.TOKENIZED:
        ...     token_id = user_selection  # e.g., "HUMOR_PRESET_OFF"
        ...     token = await token_repo.get(token_id)
        ...     content = token.content
        >>>
        >>> # STATIC section: Read from blueprint
        >>> elif section_type == SectionType.STATIC:
        ...     content = blueprint.static_sections["policies"]
        >>>
        >>> # RUNTIME section: Validate then inject
        >>> elif section_type == SectionType.RUNTIME:
        ...     result = await security_port.validate(
        ...         biographical_facts,
        ...         context="biographical_user_123",
        ...         zone=TrustZone.UNTRUSTED
        ...     )
        ...     content = result.sanitized_text
    """

    TOKENIZED = "tokenized"  # Resolved from token library via classes
    STATIC = "static"        # Stored with blueprint (few_shot, policies)
    RUNTIME = "runtime"      # Injected at request time (biographical, conversation)

    def requires_validation(self) -> bool:
        """Check if this section type requires SecurityPort validation.

        Returns:
            True for RUNTIME sections, False for TOKENIZED/STATIC

        Examples:
            >>> SectionType.TOKENIZED.requires_validation()
            False
            >>> SectionType.STATIC.requires_validation()
            False
            >>> SectionType.RUNTIME.requires_validation()
            True
        """
        return self == SectionType.RUNTIME

    def is_user_customizable(self) -> bool:
        """Check if users can customize this section type.

        Returns:
            True for TOKENIZED (user selects tokens), False for STATIC/RUNTIME

        Examples:
            >>> SectionType.TOKENIZED.is_user_customizable()
            True
            >>> SectionType.STATIC.is_user_customizable()
            False
            >>> SectionType.RUNTIME.is_user_customizable()
            False
        """
        return self == SectionType.TOKENIZED
