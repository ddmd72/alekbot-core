"""The pass-through `MockSecurityPort` that `test_firestore_token_repository_written_document_b.py` imports.

The repository tests that lived here ran the three prompt stores over a mock client; the kits
(`test_prompt_token_store_kit.py`, `test_prompt_blueprint_store_kit.py`, `test_prompt_profile_store_kit.py`) cover them.
"""

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
