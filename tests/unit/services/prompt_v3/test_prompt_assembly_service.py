"""
Unit tests for PromptAssemblyService.

Tests prompt assembly with 3 section types and 4-level resolution.
"""

import pytest
from unittest.mock import AsyncMock, Mock

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass
from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.security import SecurityPort, ValidationResult, RiskLevel, TrustZone
from src.domain.prompt_v3.profile_slot import ProfileSlot, ProfileSlotType


# Mock SecurityPort
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


@pytest.fixture
def mock_repos():
    """Create mock repositories."""
    token_repo = AsyncMock()
    blueprint_repo = AsyncMock()
    profile_repo = AsyncMock()

    # Setup token repo
    async def mock_get_token(token_id):
        return Token(
            id=token_id,
            category=TokenCategory("test_category"),
            class_=TokenClass("properties"),
            content=f"<TOKEN:{token_id}>",
            metadata={}
        )
    token_repo.get = mock_get_token

    # Setup blueprint repo
    async def mock_get_blueprint(blueprint_id):
        return Blueprint(
            id=blueprint_id,
            classes={
                "SLOT1": BlueprintClass(
                    allowed_token_categories={TokenCategory("test_category")},
                    overridable_by={OwnerType.USER},
                    default_token=TokenId("DEFAULT_TOKEN1")
                )
            },
            template="Template: {{SLOT1}} Bio: [[BIOGRAPHICAL_CONTEXT]] Convo: [[CONVERSATION_HISTORY]]"
        )
    blueprint_repo.get = mock_get_blueprint

    # Setup profile repo — must return ProfileSlot objects (not dicts)
    async def mock_get_profile_slots(*args, **kwargs):
        return [ProfileSlot(type=ProfileSlotType.TOKEN, value="DEFAULT_TOKEN1", non_overridable=False)]
    profile_repo.get_profile_slots = mock_get_profile_slots

    return token_repo, blueprint_repo, profile_repo


@pytest.mark.asyncio
async def test_prompt_assembly_basic(mock_repos):
    """Test basic prompt assembly."""
    token_repo, blueprint_repo, profile_repo = mock_repos
    security_port = MockSecurityPort()
    formatter = ContextFormatter()
    bio_formatter = Mock()
    bio_formatter.format.return_value = ""

    service = PromptAssemblyService(
        token_repo, blueprint_repo, profile_repo, security_port, formatter, bio_formatter
    )

    prompt = await service.assemble(
        agent_type="test",
        user_id="user_123",
        account_id="account_456",
        biographical_facts=[],
        conversation_history=[]
    )

    assert "Template:" in prompt
    assert "<TOKEN:DEFAULT_TOKEN1>" in prompt  # Slot replaced with token
    assert "{{SLOT1}}" not in prompt  # Placeholder removed


@pytest.mark.asyncio
async def test_prompt_assembly_with_biographical_facts(mock_repos):
    """Test prompt assembly with biographical facts."""
    token_repo, blueprint_repo, profile_repo = mock_repos
    security_port = MockSecurityPort()
    formatter = ContextFormatter()
    bio_formatter = Mock()
    bio_formatter.format.return_value = "Lives in Kyiv\nSoftware engineer"

    service = PromptAssemblyService(
        token_repo, blueprint_repo, profile_repo, security_port, formatter, bio_formatter
    )

    prompt = await service.assemble(
        agent_type="test",
        user_id="user_123",
        account_id="account_456",
        biographical_facts=["Lives in Kyiv", "Software engineer"],
        conversation_history=[]
    )

    assert "Lives in Kyiv" in prompt
    assert "Software engineer" in prompt


@pytest.mark.asyncio
async def test_prompt_assembly_with_conversation_history(mock_repos):
    """Test prompt assembly with conversation history."""
    token_repo, blueprint_repo, profile_repo = mock_repos
    security_port = MockSecurityPort()
    formatter = ContextFormatter()
    bio_formatter = Mock()
    bio_formatter.format.return_value = ""

    service = PromptAssemblyService(
        token_repo, blueprint_repo, profile_repo, security_port, formatter, bio_formatter
    )

    prompt = await service.assemble(
        agent_type="test",
        user_id="user_123",
        account_id="account_456",
        biographical_facts=[],
        conversation_history=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]
    )

    assert "User: Hello" in prompt
    assert "Assistant: Hi there!" in prompt


@pytest.mark.asyncio
async def test_prompt_assembly_validates_runtime_data(mock_repos):
    """Test that runtime data is validated via SecurityPort."""
    token_repo, blueprint_repo, profile_repo = mock_repos

    # Use a security port that tracks validate() calls
    validate_calls = []

    class TrackingSecurityPort(SecurityPort):
        async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
            validate_calls.append((text, context, zone))
            return ValidationResult(
                sanitized_text=text,
                risk_level=RiskLevel.SAFE,
                risk_score=0.0,
                patterns_detected=[],
                action_taken="passed",
                metadata={}
            )

    security_port = TrackingSecurityPort()
    formatter = ContextFormatter()
    bio_formatter = Mock()
    bio_formatter.format.return_value = "Test fact"

    service = PromptAssemblyService(
        token_repo, blueprint_repo, profile_repo, security_port, formatter, bio_formatter
    )

    await service.assemble(
        agent_type="test",
        user_id="user_123",
        account_id="account_456",
        biographical_facts=["Test fact"],
        conversation_history=[{"role": "user", "content": "Hi"}]
    )

    # Should have validated both biographical facts and conversation
    assert len(validate_calls) == 2
    assert any("biographical" in call[1] for call in validate_calls)
    assert any("conversation" in call[1] for call in validate_calls)
    assert all(call[2] == TrustZone.UNTRUSTED for call in validate_calls)


@pytest.mark.asyncio
async def test_validate_slot_assignment(mock_repos):
    """Test validate_slot_assignment() method."""
    token_repo, blueprint_repo, profile_repo = mock_repos
    security_port = MockSecurityPort()
    formatter = ContextFormatter()
    bio_formatter = Mock()
    bio_formatter.format.return_value = ""

    service = PromptAssemblyService(
        token_repo, blueprint_repo, profile_repo, security_port, formatter, bio_formatter
    )

    # Valid assignment (USER can assign to SLOT1)
    valid = await service.validate_slot_assignment(
        "test_agent_v1",
        "SLOT1",
        TokenId("DEFAULT_TOKEN1"),
        "USER"
    )

    assert valid is True

    # Invalid assignment (wrong slot name)
    valid = await service.validate_slot_assignment(
        "test_agent_v1",
        "NONEXISTENT_SLOT",
        TokenId("DEFAULT_TOKEN1"),
        "USER"
    )

    assert valid is False
