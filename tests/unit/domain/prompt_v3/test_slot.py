"""Unit tests for BlueprintClass domain model.

Tests cover:
- BlueprintClass creation
- can_assign() validation logic
- Category constraints
- Permission constraints
- Immutability
"""

import pytest
from dataclasses import FrozenInstanceError

from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass


def test_blueprint_class_creation():
    """Test BlueprintClass creation with basic attributes."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("test_category")},
        overridable_by={OwnerType.USER},
        default_token=TokenId("DEFAULT_TOKEN")
    )

    assert TokenCategory("test_category") in blueprint_class.allowed_token_categories
    assert OwnerType.USER in blueprint_class.overridable_by
    assert blueprint_class.default_token == TokenId("DEFAULT_TOKEN")


def test_can_assign_with_valid_category_and_permission():
    """Test that can_assign() allows valid category + permission."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("humor_engine")},
        overridable_by={OwnerType.USER},
        default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
    )

    token = Token(
        id=TokenId("HUMOR_PRESET_OFF"),
        category=TokenCategory("humor_engine"),
        class_=TokenClass("properties"),
        content="humor_engine { status: 'disabled' }",
        metadata={}
    )

    # USER can assign humor_engine token to HUMOR_ENGINE slot
    assert blueprint_class.can_assign(token, OwnerType.USER) is True


def test_can_assign_blocks_wrong_category():
    """Test that can_assign() blocks wrong category."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("humor_engine")},
        overridable_by={OwnerType.USER},
        default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
    )

    # Token with wrong category
    token = Token(
        id=TokenId("VOICE_FORMAL"),
        category=TokenCategory("voice"),
        class_=TokenClass("properties"),
        content="voice { style: 'formal' }",
        metadata={}
    )

    # USER cannot assign voice token to humor slot
    assert blueprint_class.can_assign(token, OwnerType.USER) is False


def test_can_assign_blocks_wrong_permission():
    """Test that can_assign() blocks wrong owner type."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("humor_engine")},
        overridable_by={OwnerType.USER},  # Only USER allowed
        default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
    )

    token = Token(
        id=TokenId("HUMOR_PRESET_OFF"),
        category=TokenCategory("humor_engine"),
        class_=TokenClass("properties"),
        content="humor_engine { status: 'disabled' }",
        metadata={}
    )

    # SYSTEM cannot override (not in overridable_by)
    assert blueprint_class.can_assign(token, OwnerType.SYSTEM) is False
    assert blueprint_class.can_assign(token, OwnerType.AGENT) is False
    assert blueprint_class.can_assign(token, OwnerType.ACCOUNT) is False


def test_can_assign_with_multiple_allowed_categories():
    """Test class with multiple allowed categories."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={
            TokenCategory("voice"),
            TokenCategory("tone"),
        },
        overridable_by={OwnerType.ACCOUNT, OwnerType.USER},
        default_token=TokenId("VOICE_CONVERSATIONAL")
    )

    voice_token = Token(
        id=TokenId("VOICE_FORMAL"),
        category=TokenCategory("voice"),
        class_=TokenClass("properties"),
        content="voice { style: 'formal' }",
        metadata={}
    )

    tone_token = Token(
        id=TokenId("TONE_FRIENDLY"),
        category=TokenCategory("tone"),
        class_=TokenClass("properties"),
        content="tone { style: 'friendly' }",
        metadata={}
    )

    # Both voice and tone categories allowed
    assert blueprint_class.can_assign(voice_token, OwnerType.USER) is True
    assert blueprint_class.can_assign(tone_token, OwnerType.USER) is True
    assert blueprint_class.can_assign(voice_token, OwnerType.ACCOUNT) is True


def test_can_assign_with_multiple_owner_types():
    """Test class with multiple allowed owner types."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("voice")},
        overridable_by={
            OwnerType.AGENT,
            OwnerType.ACCOUNT,
            OwnerType.USER
        },
        default_token=TokenId("VOICE_CONVERSATIONAL")
    )

    token = Token(
        id=TokenId("VOICE_FORMAL"),
        category=TokenCategory("voice"),
        class_=TokenClass("properties"),
        content="voice { style: 'formal' }",
        metadata={}
    )

    # AGENT, ACCOUNT, USER all allowed
    assert blueprint_class.can_assign(token, OwnerType.AGENT) is True
    assert blueprint_class.can_assign(token, OwnerType.ACCOUNT) is True
    assert blueprint_class.can_assign(token, OwnerType.USER) is True

    # SYSTEM not allowed
    assert blueprint_class.can_assign(token, OwnerType.SYSTEM) is False


def test_blueprint_class_immutability():
    """Test that BlueprintClass is immutable (frozen dataclass)."""
    blueprint_class = BlueprintClass(
        allowed_token_categories={TokenCategory("test_category")},
        overridable_by={OwnerType.USER},
        default_token=TokenId("DEFAULT_TOKEN")
    )

    # Attempt to modify slot should raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        blueprint_class.allowed_token_categories = {TokenCategory("new_category")}  # type: ignore

    with pytest.raises(FrozenInstanceError):
        blueprint_class.default_token = TokenId("NEW_DEFAULT")  # type: ignore


def test_blueprint_class_hashable():
    """Test that BlueprintClass is hashable (can be used in sets/dicts)."""
    slot1 = BlueprintClass(
        allowed_token_categories={TokenCategory("humor_engine")},
        overridable_by={OwnerType.USER},
        default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
    )

    slot2 = BlueprintClass(
        allowed_token_categories={TokenCategory("voice")},
        overridable_by={OwnerType.ACCOUNT, OwnerType.USER},
        default_token=TokenId("VOICE_CONVERSATIONAL")
    )

    # Can use in set
    slot_set = {slot1, slot2}
    assert len(slot_set) == 2

    # Can use as dict key
    slot_dict = {slot1: "humor", slot2: "voice"}
    assert slot_dict[slot1] == "humor"


def test_owner_type_enum_values():
    """Test OwnerType enum has all 4 levels."""
    assert OwnerType.SYSTEM.value == "system"
    assert OwnerType.AGENT.value == "agent"
    assert OwnerType.ACCOUNT.value == "account"
    assert OwnerType.USER.value == "user"

    # Verify all 4 levels present
    assert len(OwnerType) == 4
