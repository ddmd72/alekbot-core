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
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.agent_profile import AgentProfile
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
    """Test FirestoreBlueprintRepository.get() with v4 data model."""
    db = Mock()
    repo = FirestoreBlueprintRepository(db, "test_blueprints")

    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "blueprint_id": "test_blueprint",
        "outer_class": "Alek extends Agent",
        "class_order": ["properties", "cognitive_process", "policies"],
    }

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    blueprint = await repo.get("test_blueprint")

    assert blueprint.id == "test_blueprint"
    assert blueprint.outer_class == "Alek extends Agent"
    assert blueprint.class_order == ["properties", "cognitive_process", "policies"]


@pytest.mark.asyncio
async def test_blueprint_repository_save():
    """Test FirestoreBlueprintRepository.save() with v4 data model."""
    db = Mock()
    repo = FirestoreBlueprintRepository(db, "test_blueprints")

    blueprint = Blueprint(
        id="test_blueprint",
        outer_class="Alek extends Agent",
        class_order=["properties", "cognitive_process"],
    )

    await repo.save(blueprint)

    db.collection.assert_called_with("test_blueprints")
    db.collection.return_value.document.assert_called_with("test_blueprint")


# ========== FirestoreAgentProfileRepository Tests ==========

@pytest.mark.asyncio
async def test_agent_profile_repository_get_agent_profile():
    """Test FirestoreAgentProfileRepository.get_agent_profile() reads blueprint_id + tokens."""
    db = Mock()
    repo = FirestoreAgentProfileRepository(db, "test_profiles", "test_overrides")

    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "blueprint_id": "universal_agent_v1",
        "agent_id": "quick",
        "tokens": {
            "TOKEN1": {"order": 10},
            "TOKEN2": {"order": 20, "non_overridable": True},
        }
    }

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    profile = await repo.get_agent_profile("quick")

    assert isinstance(profile, AgentProfile)
    assert profile.blueprint_id == "universal_agent_v1"
    assert len(profile.tokens) == 2
    assert profile.tokens["TOKEN1"].order == 10
    assert profile.tokens["TOKEN1"].non_overridable is False
    assert profile.tokens["TOKEN2"].order == 20
    assert profile.tokens["TOKEN2"].non_overridable is True
    # Document lookup uses agent_id as document ID (no blueprint_id prefix)
    db.collection.return_value.document.assert_called_with("quick")


@pytest.mark.asyncio
async def test_agent_profile_repository_get_override_tokens():
    """Test FirestoreAgentProfileRepository.get_override_tokens() reads tokens map."""
    db = Mock()
    repo = FirestoreAgentProfileRepository(db, "test_profiles", "test_overrides")

    mock_doc = Mock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "blueprint_id": "test_blueprint",
        "owner_type": "USER",
        "owner_id": "user_123",
        "tokens": {
            "HUMOR_PRESET_OFF": {"order": 40},
        }
    }

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    tokens = await repo.get_override_tokens(OwnerType.USER, "user_123")

    assert len(tokens) == 1
    assert tokens["HUMOR_PRESET_OFF"].order == 40


@pytest.mark.asyncio
async def test_agent_profile_repository_returns_empty_when_not_found():
    """Test FirestoreAgentProfileRepository returns empty profile when not found."""
    db = Mock()
    repo = FirestoreAgentProfileRepository(db, "test_profiles", "test_overrides")

    mock_doc = Mock()
    mock_doc.exists = False

    db.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

    profile = await repo.get_agent_profile("quick")
    assert profile.tokens == {}
    assert profile.blueprint_id == "quick_agent_v1"  # derived fallback
