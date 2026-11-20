"""
Unit tests for SecurityPort: a concrete subclass with validate() works; domain types (RiskLevel, TrustZone,
ValidationResult) are accessible from ports/security_port.
"""


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


class TestSecurityPortABC:
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
