"""
Unit tests for Firestore repositories (Phase 2).

Uses mock Firestore client for fast unit testing.
"""

import pytest
from unittest.mock import Mock, AsyncMock

from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass
from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.security import SecurityPort, ValidationResult, RiskLevel, TrustZone


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


# ========== FirestoreTokenRepository Tests ==========

@pytest.mark.asyncio
async def test_token_repository_get():
    """Test FirestoreTokenRepository.get() with dual-collection lookup"""
    db = Mock()
    security_port = MockSecurityPort()
    repo = FirestoreTokenRepository(db, "test_system_tokens", "test_user_tokens", security_port)

    # Mock Firestore document (found in system collection)
    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "token_id": "TEST_TOKEN",
        "category": "test_category",
        "class": "properties",
        "content": "test content",
        "metadata": {"version": "1.0"}
    }

    # Firestore .get() is async — use AsyncMock
    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    # Test get
    token = await repo.get(TokenId("TEST_TOKEN"))

    assert token.id == "TEST_TOKEN"
    assert token.category == "test_category"
    assert token.content == "test content"
    db.collection.assert_called_with("test_system_tokens")


@pytest.mark.asyncio
async def test_token_repository_get_not_found():
    """Test FirestoreTokenRepository.get() raises KeyError if not found in both collections"""
    db = Mock()
    security_port = MockSecurityPort()
    repo = FirestoreTokenRepository(db, "test_system_tokens", "test_user_tokens", security_port)

    mock_doc = Mock()
    mock_doc.exists = False

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    with pytest.raises(KeyError, match="Token not found"):
        await repo.get(TokenId("NONEXISTENT"))


@pytest.mark.asyncio
async def test_token_repository_save():
    """Test FirestoreTokenRepository.save() to system collection"""
    db = Mock()
    security_port = MockSecurityPort()
    repo = FirestoreTokenRepository(db, "test_system_tokens", "test_user_tokens", security_port)

    token = Token(
        id=TokenId("TEST_TOKEN"),
        category=TokenCategory("test_category"),
        class_=TokenClass("properties"),
        content="test content",
        metadata={"version": "1.0"}
    )

    await repo.save(token, collection="system")

    db.collection.assert_called_with("test_system_tokens")
    db.collection.return_value.document.assert_called_with("TEST_TOKEN")
    db.collection.return_value.document.return_value.set.assert_called_once()


# ========== FirestoreBlueprintRepository Tests ==========

@pytest.mark.asyncio
async def test_blueprint_repository_get():
    """Test FirestoreBlueprintRepository.get()"""
    db = Mock()
    repo = FirestoreBlueprintRepository(db, "test_blueprints")

    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "blueprint_id": "test_blueprint",
        "template": "{{SLOT1}}",
        "classes": {
            "SLOT1": {
                "allowed_token_categories": ["category1"],
                "overridable_by": ["user"],
                "default_token": "TOKEN1"
            }
        }
    }

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    blueprint = await repo.get("test_blueprint")

    assert blueprint.id == "test_blueprint"
    assert "SLOT1" in blueprint.classes
    assert blueprint.template == "{{SLOT1}}"


@pytest.mark.asyncio
async def test_blueprint_repository_save():
    """Test FirestoreBlueprintRepository.save()"""
    db = Mock()
    repo = FirestoreBlueprintRepository(db, "test_blueprints")

    blueprint = Blueprint(
        id="test_blueprint",
        classes={
            "SLOT1": BlueprintClass(
                allowed_token_categories={TokenCategory("category1")},
                overridable_by={OwnerType.USER},
                default_token=TokenId("TOKEN1")
            )
        },
        template="{{SLOT1}}"
    )

    await repo.save(blueprint)

    db.collection.assert_called_with("test_blueprints")
    db.collection.return_value.document.assert_called_with("test_blueprint")


# ========== FirestoreAgentProfileRepository Tests ==========

@pytest.mark.asyncio
async def test_agent_profile_repository_get_profile_slots():
    """Test FirestoreAgentProfileRepository.get_profile_slots()"""
    db = Mock()
    repo = FirestoreAgentProfileRepository(db, "test_profiles", "test_overrides")

    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "slots": [
            {"type": "token", "value": "TOKEN1", "non_overridable": False},
            {"type": "token", "value": "TOKEN2", "non_overridable": False}
        ]
    }

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    slots = await repo.get_profile_slots(
        "test_blueprint",
        OwnerType.USER,
        "user_123"
    )

    assert len(slots) == 2
    assert slots[0].value == "TOKEN1"
    assert slots[1].value == "TOKEN2"


# Test removed - resolve_slot_assignments() deprecated in favor of get_profile_slots()
# 4-level resolution now handled by PromptAssemblyService._resolve_profile_slots()
