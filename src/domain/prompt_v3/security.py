"""
Security domain types — RiskLevel, TrustZone, ValidationResult.

Part of Prompt Design System v3 (RFC).
SecurityPort (ABC) lives in src/ports/security_port.py.
"""

from dataclasses import dataclass
from enum import Enum


class RiskLevel(Enum):
    """Risk classification for validation results."""
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TrustZone(Enum):
    """Trust classification for different content sources.

    Used to prevent recursion and optimize validation:
    - UNTRUSTED: User input, model output → full validation required
    - SEMI_TRUSTED: RAG content, enriched search → moderate validation
    - TRUSTED: System prompts, agent instructions → skip validation
    """
    UNTRUSTED = "untrusted"      # User input, model output
    SEMI_TRUSTED = "semi_trusted"  # RAG content, enriched search
    TRUSTED = "trusted"          # System prompts (skip validation)


@dataclass
class ValidationResult:
    """Result of security validation.

    Attributes:
        sanitized_text: Safe text after sanitization
        risk_level: Risk classification (SAFE, LOW, MEDIUM, HIGH, CRITICAL)
        risk_score: Normalized risk score (0.0 = safe, 1.0 = critical)
        patterns_detected: List of pattern names matched (for logging)
        action_taken: Action performed ("passed", "sanitized", "blocked")
        metadata: Adapter-specific metadata (e.g., adapter name, context)

    Examples:
        >>> result = ValidationResult(
        ...     sanitized_text="Hello world",
        ...     risk_level=RiskLevel.SAFE,
        ...     risk_score=0.0,
        ...     patterns_detected=[],
        ...     action_taken="passed",
        ...     metadata={"adapter": "regex", "zone": "untrusted"}
        ... )
    """
    sanitized_text: str
    risk_level: RiskLevel
    risk_score: float  # 0.0-1.0 normalized risk
    patterns_detected: list[str]  # Matched patterns (for logging)
    action_taken: str  # "passed", "sanitized", "blocked"
    metadata: dict  # Adapter-specific metadata


