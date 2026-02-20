"""
Unit tests for Blueprint domain model.

Tests cover:
- Blueprint creation
- can_assign() validation
- Template validation (ensure all slots exist)
- get_required_slots() extraction
- Immutability
"""

import pytest
from dataclasses import FrozenInstanceError

from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.slot import BlueprintClass, OwnerType
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass


def create_test_blueprint():
    """Helper to create a test blueprint."""
    return Blueprint(
        id="test_blueprint",
        classes={
            "HUMOR_ENGINE": BlueprintClass(
                allowed_token_categories={TokenCategory("humor_engine")},
                overridable_by={OwnerType.USER},
                default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
            ),
            "VOICE": BlueprintClass(
                allowed_token_categories={TokenCategory("voice"), TokenCategory("tone")},
                overridable_by={OwnerType.ACCOUNT, OwnerType.USER},
                default_token=TokenId("VOICE_CONVERSATIONAL")
            ),
        },
        template="""class Alek {
    properties {
        {{HUMOR_ENGINE}}
        {{VOICE}}
    }
}"""
    )


def test_blueprint_creation():
    """Test Blueprint creation with classes and template."""
    blueprint = create_test_blueprint()

    assert blueprint.id == "test_blueprint"
    assert "HUMOR_ENGINE" in blueprint.classes
    assert "VOICE" in blueprint.classes
    assert "{{HUMOR_ENGINE}}" in blueprint.template
    assert "{{VOICE}}" in blueprint.template


def test_can_assign_with_valid_slot_and_permission():
    """Test that can_assign() allows valid class + token + permission."""
    blueprint = create_test_blueprint()

    humor_token = Token(
        id=TokenId("HUMOR_PRESET_OFF"),
        category=TokenCategory("humor_engine"),
        class_=TokenClass("properties"),
        content="humor_engine { status: 'disabled' }",
        metadata={}
    )

    # USER can assign humor_engine token to HUMOR_ENGINE class
    assert blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.USER) is True


def test_can_assign_blocks_nonexistent_slot():
    """Test that can_assign() blocks nonexistent class."""
    blueprint = create_test_blueprint()

    token = Token(
        id=TokenId("TEST_TOKEN"),
        category=TokenCategory("test_category"),
        class_=TokenClass("properties"),
        content="test content",
        metadata={}
    )

    # Class doesn't exist
    assert blueprint.can_assign("NONEXISTENT_SLOT", token, OwnerType.USER) is False


def test_can_assign_blocks_wrong_category():
    """Test that can_assign() blocks wrong category via BlueprintClass."""
    blueprint = create_test_blueprint()

    # Token with voice category
    voice_token = Token(
        id=TokenId("VOICE_FORMAL"),
        category=TokenCategory("voice"),
        class_=TokenClass("properties"),
        content="voice { style: 'formal' }",
        metadata={}
    )

    # Cannot assign voice token to HUMOR_ENGINE class
    assert blueprint.can_assign("HUMOR_ENGINE", voice_token, OwnerType.USER) is False


def test_can_assign_blocks_wrong_permission():
    """Test that can_assign() blocks wrong permission via BlueprintClass."""
    blueprint = create_test_blueprint()

    humor_token = Token(
        id=TokenId("HUMOR_PRESET_OFF"),
        category=TokenCategory("humor_engine"),
        class_=TokenClass("properties"),
        content="humor_engine { status: 'disabled' }",
        metadata={}
    )

    # SYSTEM cannot override HUMOR_ENGINE class (only USER allowed)
    assert blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.SYSTEM) is False
    assert blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.AGENT) is False
    assert blueprint.can_assign("HUMOR_ENGINE", humor_token, OwnerType.ACCOUNT) is False


def test_validate_passes_with_all_slots_defined():
    """Test that validate() passes when all template classes are defined."""
    blueprint = create_test_blueprint()

    # Should not raise (all slots defined)
    blueprint.validate()


def test_validate_fails_with_undefined_slots():
    """Test that validate() raises ValueError for undefined classes in template."""
    blueprint = Blueprint(
        id="test_blueprint",
        classes={
            "HUMOR_ENGINE": BlueprintClass(
                allowed_token_categories={TokenCategory("humor_engine")},
                overridable_by={OwnerType.USER},
                default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
            ),
        },
        template="{{HUMOR_ENGINE}} {{UNDEFINED_SLOT}} {{ANOTHER_UNDEFINED}}"
    )

    with pytest.raises(ValueError, match="Template references undefined classes"):
        blueprint.validate()


def test_get_required_classes():
    """Test get_required_classes() extracts all class names from template."""
    blueprint = create_test_blueprint()

    required_classes = blueprint.get_required_classes()

    assert required_classes == {"HUMOR_ENGINE", "VOICE"}


def test_get_required_classes_with_multiple_references():
    """Test get_required_classes() handles duplicate class references."""
    blueprint = Blueprint(
        id="test_blueprint",
        classes={
            "HUMOR_ENGINE": BlueprintClass(
                allowed_token_categories={TokenCategory("humor_engine")},
                overridable_by={OwnerType.USER},
                default_token=TokenId("HUMOR_PRESET_RANEVSKAYA")
            ),
        },
        template="{{HUMOR_ENGINE}} some text {{HUMOR_ENGINE}}"
    )

    required_classes = blueprint.get_required_classes()

    # Should only return unique slot names
    assert required_classes == {"HUMOR_ENGINE"}


def test_get_required_classes_empty_template():
    """Test get_required_classes() with template containing no classes."""
    blueprint = Blueprint(
        id="test_blueprint",
        classes={},
        template="class Alek { properties { } }"
    )

    required_classes = blueprint.get_required_classes()

    assert required_classes == set()


def test_blueprint_immutability():
    """Test that Blueprint is immutable (frozen dataclass)."""
    blueprint = create_test_blueprint()

    # Attempt to modify blueprint should raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        blueprint.id = "new_id"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        blueprint.template = "new template"  # type: ignore


def test_blueprint_hashable():
    """Test that Blueprint is hashable (can be used in sets/dicts)."""
    blueprint1 = create_test_blueprint()

    blueprint2 = Blueprint(
        id="another_blueprint",
        classes={},
        template="{{SLOT}}"
    )

    # Can use in set
    blueprint_set = {blueprint1, blueprint2}
    assert len(blueprint_set) == 2

    # Can use as dict key
    blueprint_dict = {blueprint1: "test1", blueprint2: "test2"}
    assert blueprint_dict[blueprint1] == "test1"


def test_can_assign_with_multiple_allowed_categories():
    """Test can_assign() works with classes allowing multiple categories."""
    blueprint = create_test_blueprint()

    # VOICE class allows both "voice" and "tone" categories
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

    # Both should be allowed
    assert blueprint.can_assign("VOICE", voice_token, OwnerType.USER) is True
    assert blueprint.can_assign("VOICE", tone_token, OwnerType.USER) is True
