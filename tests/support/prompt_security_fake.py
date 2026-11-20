"""A pass-through `SecurityPort` fake: every text is returned unchanged and rated safe."""

from src.ports.security_port import SecurityPort, ValidationResult, RiskLevel, TrustZone


# Mock SecurityPort for testing
class MockSecurityPort(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(
            sanitized_text=text,
            risk_level=RiskLevel.SAFE,
            risk_score=0.0,
            patterns_detected=[],
            action_taken="passed",
            metadata={"adapter": "mock"}
        )
