"""
ExternalAPIAdapter - External API validation (PLACEHOLDER).

Examples: Perspective API, Azure Content Safety, custom service.
TODO: Implement HTTP client call to external validation service.
MVP: Falls back to injected SecurityPort fallback.
"""

import logging
from typing import Optional

from src.ports.security_port import (
    SecurityPort,
    ValidationResult,
    TrustZone,
)

logger = logging.getLogger(__name__)


class ExternalAPIAdapter(SecurityPort):
    """External API validation (PLACEHOLDER for future integration).

    Examples: Perspective API, Azure Content Safety, custom service.
    TODO (Phase 6+): Implement HTTP client call.
    MVP: Falls back to injected SecurityPort fallback.

    Future Implementation:
        - HTTP client with retry logic
        - API key management
        - Rate limiting
        - Timeout handling
        - Error recovery

    Examples:
        >>> # MVP: Uses injected fallback
        >>> from src.adapters.security.regex_adapter import RegexSecurityAdapter
        >>> adapter = ExternalAPIAdapter(
        ...     fallback=RegexSecurityAdapter(),
        ...     api_url="https://api.example.com/validate",
        ...     api_key="secret"
        ... )
        >>> result = await adapter.validate("Hello", "test", TrustZone.UNTRUSTED)
        >>> # Future: Will call external API
    """

    def __init__(
        self,
        fallback: SecurityPort,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """Initialize ExternalAPIAdapter with injected fallback.

        Args:
            fallback: SecurityPort implementation used as fallback until external API is implemented
            api_url: External API endpoint URL (not used in MVP)
            api_key: API authentication key (not used in MVP)
        """
        self.api_url = api_url
        self.api_key = api_key
        self._fallback = fallback

        if api_url:
            logger.warning(
                f"ExternalAPIAdapter configured with api_url={api_url}, "
                "but external API not implemented yet. Using fallback."
            )
        else:
            logger.info(
                "ExternalAPIAdapter initialized with fallback (placeholder mode)"
            )

    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED
    ) -> ValidationResult:
        """TODO: Call external API for validation.

        Placeholder implementation: Use regex fallback.

        Args:
            text: Text to validate
            context: Context for logging
            zone: Trust zone classification

        Returns:
            ValidationResult from regex fallback

        Examples:
            >>> adapter = ExternalAPIAdapter()
            >>> result = await adapter.validate("Hello", "test", TrustZone.UNTRUSTED)
            >>> # Currently uses regex, future will call external API
        """
        logger.debug(
            "ExternalAPIAdapter not implemented, using regex fallback "
            f"(context={context})"
        )

        # TODO (Phase 6+): Implement HTTP call
        # import httpx
        # async with httpx.AsyncClient() as client:
        #     response = await client.post(
        #         self.api_url,
        #         json={"text": text, "context": context},
        #         headers={"Authorization": f"Bearer {self.api_key}"},
        #         timeout=5.0
        #     )
        #     risk_data = response.json()
        #     return ValidationResult(
        #         sanitized_text=risk_data["sanitized_text"],
        #         risk_level=RiskLevel[risk_data["risk_level"].upper()],
        #         risk_score=risk_data["risk_score"],
        #         patterns_detected=risk_data.get("patterns", []),
        #         action_taken=risk_data["action"],
        #         metadata={"adapter": "external_api", "api_url": self.api_url}
        #     )

        # MVP: Use regex fallback
        result = await self._fallback.validate(text, context, zone)

        # Add metadata to indicate this was a fallback
        result.metadata["external_api_adapter"] = "fallback_to_regex"
        if self.api_url:
            result.metadata["configured_api_url"] = self.api_url

        return result
