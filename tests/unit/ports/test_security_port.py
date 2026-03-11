"""
Unit tests for SecurityPort — verifies ABC contract and domain type availability.

Covers:
- SecurityPort is an ABC (cannot be instantiated directly)
- Concrete subclass without validate() raises TypeError
- Concrete subclass with validate() works correctly
- Domain types (RiskLevel, TrustZone, ValidationResult) accessible from ports/security_port
"""

import pytest

from src.ports.security_port import (
    SecurityPort,
    RiskLevel,
    TrustZone,
    ValidationResult,
)


class ConcreteSecurityPort(SecurityPort):
    """Minimal valid implementation for testing."""

    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(
            sanitized_text=text,
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={"adapter": "test"},
        )


class IncompleteSecurityPort(SecurityPort):
    """Missing validate() — should fail to instantiate."""
    pass


class TestSecurityPortABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            SecurityPort()

    def test_incomplete_subclass_raises_on_instantiation(self):
        with pytest.raises(TypeError):
            IncompleteSecurityPort()

    def test_complete_subclass_instantiates(self):
        port = ConcreteSecurityPort()
        assert isinstance(port, SecurityPort)

    async def test_validate_returns_validation_result(self):
        port = ConcreteSecurityPort()
        result = await port.validate("hello", "ctx")
        assert isinstance(result, ValidationResult)
        assert result.risk_level == RiskLevel.SAFE
        assert result.sanitized_text == "hello"

    async def test_validate_default_zone_is_untrusted(self):
        captured = {}

        class ZoneCapture(SecurityPort):
            async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
                captured["zone"] = zone
                return ValidationResult(
                    sanitized_text=text,
                    risk_level=RiskLevel.SAFE,
                    risk_score=0.0,
                    patterns_detected=[],
                    action_taken="passed",
                    metadata={},
                )

        await ZoneCapture().validate("x", "ctx")
        assert captured["zone"] == TrustZone.UNTRUSTED


class TestDomainTypesReexported:
    """Domain types must be importable from ports/security_port (re-export convenience)."""

    def test_risk_level_values(self):
        assert RiskLevel.SAFE.value == "safe"
        assert RiskLevel.CRITICAL.value == "critical"

    def test_trust_zone_values(self):
        assert TrustZone.UNTRUSTED.value == "untrusted"
        assert TrustZone.TRUSTED.value == "trusted"

    def test_validation_result_is_dataclass(self):
        result = ValidationResult(
            sanitized_text="ok",
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={},
        )
        assert result.sanitized_text == "ok"
        assert result.risk_score == 0.0
