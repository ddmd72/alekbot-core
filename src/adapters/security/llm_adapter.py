"""
LLMSecurityAdapter - LLM-based semantic risk assessment (PLACEHOLDER).

TODO: Implement LLM judge for semantic prompt injection detection.
MVP: Falls back to RegexSecurityAdapter.
"""

import logging

from src.domain.prompt_v3.security import (
    SecurityPort,
    ValidationResult,
    TrustZone,
)
from src.adapters.security.regex_adapter import RegexSecurityAdapter

logger = logging.getLogger(__name__)


class LLMSecurityAdapter(SecurityPort):
    """LLM-based semantic risk assessment (PLACEHOLDER for future implementation).

    TODO (Phase 6+): Implement RiskAssessmentAgent integration.
    MVP: Falls back to RegexSecurityAdapter.

    Future Implementation:
        - Use RiskAssessmentAgent with hardcoded prompt
        - Semantic analysis (not just pattern matching)
        - Detect indirect injection attempts
        - No PromptAssembly dependency (prevent recursion)

    Examples:
        >>> # MVP: Uses regex fallback
        >>> adapter = LLMSecurityAdapter()
        >>> result = await adapter.validate("Hello", "test", TrustZone.UNTRUSTED)
        >>> # Future: Will use LLM judge for semantic analysis
    """

    def __init__(self):
        """Initialize LLMSecurityAdapter with regex fallback."""
        self._fallback = RegexSecurityAdapter()
        logger.info(
            "LLMSecurityAdapter initialized with regex fallback (placeholder mode)"
        )

    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED
    ) -> ValidationResult:
        """TODO: Call RiskAssessmentAgent with hardcoded prompt.

        Placeholder implementation: Use regex fallback.

        Args:
            text: Text to validate
            context: Context for logging
            zone: Trust zone classification

        Returns:
            ValidationResult from regex fallback

        Examples:
            >>> adapter = LLMSecurityAdapter()
            >>> result = await adapter.validate("Hello", "test", TrustZone.UNTRUSTED)
            >>> # Currently uses regex, future will use LLM judge
        """
        logger.debug(
            "LLMSecurityAdapter not implemented, using regex fallback "
            f"(context={context})"
        )

        # TODO (Phase 6+): Implement RiskAssessmentAgent call
        # from src.domain.prompt_v3.risk_agent import RiskAssessmentAgent
        # risk_agent = RiskAssessmentAgent(llm_client)
        # assessment = await risk_agent.assess(text, zone)
        # return ValidationResult(
        #     sanitized_text=text if assessment["risk_level"] == "safe" else "[BLOCKED]",
        #     risk_level=RiskLevel[assessment["risk_level"].upper()],
        #     risk_score=assessment["risk_score"],
        #     patterns_detected=assessment["detected_techniques"],
        #     action_taken="passed" if assessment["risk_level"] == "safe" else "blocked",
        #     metadata={"adapter": "llm", "reasoning": assessment["reasoning"]}
        # )

        # MVP: Use regex fallback
        result = await self._fallback.validate(text, context, zone)

        # Add metadata to indicate this was a fallback
        result.metadata["llm_adapter"] = "fallback_to_regex"

        return result
