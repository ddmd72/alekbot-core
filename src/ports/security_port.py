"""SecurityPort - Port interface for extensible security validation.

ABCs belong in ports/; domain-side types (RiskLevel, TrustZone, ValidationResult)
remain in src/domain/prompt_v3/security.py and are re-exported here for convenience.
"""

from abc import ABC, abstractmethod

from src.domain.prompt_v3.security import RiskLevel, TrustZone, ValidationResult

__all__ = ["SecurityPort", "RiskLevel", "TrustZone", "ValidationResult"]


class SecurityPort(ABC):
    """Port interface for extensible security validation.

    Implementations live in src/adapters/security/:
        - RegexSecurityAdapter: Pattern-based validation (MVP)
        - LLMSecurityAdapter: LLM-based semantic risk assessment (future)
        - ExternalAPIAdapter: External service validation (future)
        - CompositeAdapter: Aggregates multiple adapters (MVP)
    """

    @abstractmethod
    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED,
    ) -> ValidationResult:
        """Validate text and return sanitized version + risk assessment.

        Args:
            text: Text to validate (user input, model output, RAG content, etc.)
            context: Context for logging (e.g., "user_input_user_123", "token_creation")
            zone: Trust zone classification (default: UNTRUSTED)

        Returns:
            ValidationResult with sanitized text and risk assessment

        Raises:
            ValueError: If validation fails (implementation-specific)
        """
