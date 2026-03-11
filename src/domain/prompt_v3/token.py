"""
Token - Immutable, pre-approved prompt fragment.

Part of Prompt Design System v3 (RFC).
"""

from dataclasses import dataclass
from typing import NewType

TokenId = NewType('TokenId', str)  # e.g., "HUMOR_PRESET_RANEVSKAYA"
TokenCategory = NewType('TokenCategory', str)  # e.g., "humor_engine", "cognitive_process"
TokenClass = NewType('TokenClass', str)  # Groovy section name, e.g., "properties", "policies"


@dataclass(frozen=True)
class Token:
    """Immutable, pre-approved prompt fragment.

    IMPORTANT: Use Token.create() factory method for validation.
    Direct instantiation bypasses security checks.

    Examples:
        >>> # ❌ WRONG: Direct instantiation (bypasses validation)
        >>> token = Token(
        ...     id=TokenId("HUMOR_OFF"),
        ...     category=TokenCategory("humor_engine"),
        ...     content="humor_engine { status: 'disabled' }",
        ...     metadata={}
        ... )

        >>> # ✅ CORRECT: Factory method with validation
        >>> token = await Token.create(
        ...     id=TokenId("HUMOR_OFF"),
        ...     category=TokenCategory("humor_engine"),
        ...     content="humor_engine { status: 'disabled' }",
        ...     metadata={"version": "1.0"},
        ...     security_port=security_port
        ... )
    """

    id: TokenId
    category: TokenCategory  # semantic group, used as dedup key during override resolution
    class_: TokenClass       # Groovy section this token renders into (e.g., "properties")
    content: str             # bare Groovy block content, WITHOUT the outer section wrapper
    metadata: dict           # version, author, description, validation results

    @classmethod
    async def create(
        cls,
        id: TokenId,
        category: TokenCategory,
        class_: TokenClass,
        content: str,
        metadata: dict,
        security_port: 'SecurityPort'
    ) -> "Token":
        """Factory method with SecurityPort validation.

        Validates token content using the same SecurityPort used for runtime data.
        This ensures consistency across all validation points.

        Args:
            id: Unique token identifier (e.g., "HUMOR_PRESET_RANEVSKAYA")
            category: Token category (e.g., "humor_engine", "cognitive_process")
            class_: Blueprint section class (e.g., "properties", "policies")
            content: Groovy code block to validate and store
            metadata: Additional metadata (version, author, description)
            security_port: SecurityPort instance for validation

        Returns:
            Validated Token instance with sanitized content

        Raises:
            ValueError: If validation fails (HIGH or CRITICAL risk level detected)

        Examples:
            >>> from src.domain.prompt_v3.security import TrustZone, RiskLevel
            >>> # security_port = RegexSecurityAdapter()  # from adapters layer
            >>> token = await Token.create(
            ...     id=TokenId("HUMOR_OFF"),
            ...     category=TokenCategory("humor_engine"),
            ...     class_=TokenClass("properties"),
            ...     content="humor_engine { status: 'disabled' }",
            ...     metadata={"version": "1.0", "author": "system"},
            ...     security_port=security_port
            ... )
        """
        from src.domain.prompt_v3.security import TrustZone, RiskLevel

        # Validate token content at creation time
        result = await security_port.validate(
            text=content,
            context="token_creation",
            zone=TrustZone.TRUSTED  # Tokens created by admins are semi-trusted
        )

        if result.risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
            raise ValueError(
                f"Token {id} content validation failed: {result.patterns_detected}"
            )

        # Use sanitized content (in case MEDIUM patterns were cleaned)
        return cls(
            id=id,
            category=category,
            class_=class_,
            content=result.sanitized_text,
            metadata={
                **metadata,
                "validation": {
                    "risk_level": result.risk_level.value,
                    "risk_score": result.risk_score,
                    "patterns_detected": result.patterns_detected,
                    "action_taken": result.action_taken,
                }
            }
        )
