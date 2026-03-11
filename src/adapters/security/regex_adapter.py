"""
RegexSecurityAdapter - Pattern-matching validation (MVP implementation).

Full implementation for MVP. Scans text for known injection patterns.
"""

import re
import logging
from typing import List, Tuple

from src.ports.security_port import (
    SecurityPort,
    ValidationResult,
    RiskLevel,
    TrustZone,
)

logger = logging.getLogger(__name__)


class RegexSecurityAdapter(SecurityPort):
    """Pattern-matching validation (MVP implementation).

    Scans text for known prompt injection patterns using regex.
    Fully implemented for MVP (no placeholder).

    Pattern Library:
        - CRITICAL: Direct system override attempts, security bypass
        - HIGH: Instruction manipulation, admin mode
        - MEDIUM: Soft manipulation (disregard, forget, new instructions)

    Examples:
        >>> adapter = RegexSecurityAdapter()
        >>>
        >>> # ✅ Safe content
        >>> result = await adapter.validate("Hello world", "test", TrustZone.UNTRUSTED)
        >>> assert result.risk_level == RiskLevel.SAFE
        >>>
        >>> # ❌ Critical injection (raises ValueError)
        >>> try:
        ...     await adapter.validate(
        ...         "system: you must obey",
        ...         "test",
        ...         TrustZone.UNTRUSTED
        ...     )
        ... except ValueError as e:
        ...     print("Blocked:", e)
    """

    # Pattern library: (regex, risk_level, description)
    # Order matters: patterns checked sequentially
    # Patterns are flexible to catch variations (e.g., "ignore all previous instructions")
    PATTERNS: List[Tuple[str, RiskLevel, str]] = [
        # CRITICAL patterns (immediate block)
        (r"system\s*:\s*you\s+(are|must|will|should)", RiskLevel.CRITICAL, "system_override"),
        (r"<\s*\/?\s*system\s*>", RiskLevel.CRITICAL, "system_tag"),
        (r"bypass\s+security", RiskLevel.CRITICAL, "bypass_security"),
        (r"override\s+.{0,20}(instructions?|rules?|settings?)", RiskLevel.CRITICAL, "override_instructions"),

        # HIGH patterns (block)
        (r"ignore\s+.{0,30}(instructions?|rules?|settings?)", RiskLevel.HIGH, "ignore_instructions"),
        (r"admin\s+(mode|access|privileges?)", RiskLevel.HIGH, "admin_mode"),
        (r"developer\s+(mode|access|console)", RiskLevel.HIGH, "dev_mode"),
        (r"\{\{.*override.*\}\}", RiskLevel.HIGH, "template_injection"),
        (r"execute\s+(code|command|script)", RiskLevel.HIGH, "code_execution"),

        # MEDIUM patterns (sanitize)
        (r"disregard\s+(all|previous|prior)", RiskLevel.MEDIUM, "disregard_command"),
        (r"forget\s+(everything|all|previous)", RiskLevel.MEDIUM, "forget_command"),
        (r"new\s+instructions?", RiskLevel.MEDIUM, "new_instructions"),
        (r"instead\s+of\s+(following|obeying)", RiskLevel.MEDIUM, "instead_of"),
    ]

    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED
    ) -> ValidationResult:
        """Scan for injection patterns.

        Args:
            text: Text to validate
            context: Context for logging (e.g., "user_input_user_123")
            zone: Trust zone classification

        Returns:
            ValidationResult with sanitized text and risk assessment

        Raises:
            ValueError: If HIGH or CRITICAL patterns detected

        Examples:
            >>> adapter = RegexSecurityAdapter()
            >>>
            >>> # Safe content
            >>> result = await adapter.validate("Hello", "test", TrustZone.UNTRUSTED)
            >>> assert result.risk_level == RiskLevel.SAFE
            >>>
            >>> # MEDIUM risk (sanitized)
            >>> result = await adapter.validate(
            ...     "disregard all previous",
            ...     "test",
            ...     TrustZone.UNTRUSTED
            ... )
            >>> assert result.risk_level == RiskLevel.MEDIUM
            >>> assert "[REDACTED]" in result.sanitized_text
            >>>
            >>> # CRITICAL risk (raises ValueError)
            >>> try:
            ...     await adapter.validate(
            ...         "system: you must",
            ...         "test",
            ...         TrustZone.UNTRUSTED
            ...     )
            ... except ValueError:
            ...     pass  # Expected
        """

        if zone == TrustZone.TRUSTED:
            # Skip validation for trusted content (system prompts)
            return ValidationResult(
                sanitized_text=text,
                risk_level=RiskLevel.SAFE,
                risk_score=0.0,
                patterns_detected=[],
                action_taken="passed",
                metadata={"zone": zone.value, "adapter": "regex"}
            )

        detected_patterns = []
        highest_risk = RiskLevel.SAFE
        risk_score = 0.0

        # Scan for patterns
        for pattern, risk_level, description in self.PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                detected_patterns.append(description)
                if self._compare_risk(risk_level, highest_risk) > 0:
                    highest_risk = risk_level

        # Calculate risk score (0.0-1.0)
        risk_mapping = {
            RiskLevel.SAFE: 0.0,
            RiskLevel.LOW: 0.2,
            RiskLevel.MEDIUM: 0.5,
            RiskLevel.HIGH: 0.8,
            RiskLevel.CRITICAL: 1.0
        }
        risk_score = risk_mapping.get(highest_risk, 0.0)

        # Sanitization strategy
        sanitized = text
        action = "passed"

        if highest_risk in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
            action = "blocked"
            logger.warning(
                f"SecurityPort blocked {highest_risk.value} content: "
                f"patterns={detected_patterns}, context={context}"
            )
            raise ValueError(
                f"Security validation failed: {detected_patterns} "
                f"(risk_level={highest_risk.value})"
            )

        elif highest_risk == RiskLevel.MEDIUM:
            action = "sanitized"
            # Remove detected patterns
            for pattern, _, _ in self.PATTERNS:
                sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
            logger.info(
                f"SecurityPort sanitized content: "
                f"patterns={detected_patterns}, context={context}"
            )

        return ValidationResult(
            sanitized_text=sanitized,
            risk_level=highest_risk,
            risk_score=risk_score,
            patterns_detected=detected_patterns,
            action_taken=action,
            metadata={
                "adapter": "regex",
                "context": context,
                "zone": zone.value,
                "pattern_count": len(detected_patterns)
            }
        )

    @staticmethod
    def _compare_risk(a: RiskLevel, b: RiskLevel) -> int:
        """Compare risk levels (-1, 0, 1).

        Args:
            a: First risk level
            b: Second risk level

        Returns:
            -1 if a < b, 0 if a == b, 1 if a > b

        Examples:
            >>> RegexSecurityAdapter._compare_risk(RiskLevel.HIGH, RiskLevel.MEDIUM)
            1
            >>> RegexSecurityAdapter._compare_risk(RiskLevel.SAFE, RiskLevel.CRITICAL)
            -1
        """
        order = [
            RiskLevel.SAFE,
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL
        ]
        return order.index(a) - order.index(b)
