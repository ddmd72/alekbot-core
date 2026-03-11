"""
Unit tests for SectionType enum.

Tests cover:
- Enum values
- requires_validation() logic
- is_user_customizable() logic
"""

import pytest

from src.domain.prompt_v3.section import SectionType


def test_section_type_enum_values():
    """Test SectionType has all 3 types."""
    assert SectionType.TOKENIZED.value == "tokenized"
    assert SectionType.STATIC.value == "static"
    assert SectionType.RUNTIME.value == "runtime"

    # Verify all 3 types present
    assert len(SectionType) == 3


def test_requires_validation_for_runtime():
    """Test that only RUNTIME sections require validation."""
    assert SectionType.RUNTIME.requires_validation() is True


def test_requires_validation_for_tokenized():
    """Test that TOKENIZED sections don't require validation."""
    assert SectionType.TOKENIZED.requires_validation() is False


def test_requires_validation_for_static():
    """Test that STATIC sections don't require validation."""
    assert SectionType.STATIC.requires_validation() is False


def test_is_user_customizable_for_tokenized():
    """Test that TOKENIZED sections are user customizable."""
    assert SectionType.TOKENIZED.is_user_customizable() is True


def test_is_user_customizable_for_static():
    """Test that STATIC sections are not user customizable."""
    assert SectionType.STATIC.is_user_customizable() is False


def test_is_user_customizable_for_runtime():
    """Test that RUNTIME sections are not user customizable."""
    assert SectionType.RUNTIME.is_user_customizable() is False


def test_section_type_equality():
    """Test SectionType enum equality."""
    assert SectionType.TOKENIZED == SectionType.TOKENIZED
    assert SectionType.TOKENIZED != SectionType.STATIC
    assert SectionType.TOKENIZED != SectionType.RUNTIME


def test_section_type_iteration():
    """Test iterating over all SectionType values."""
    all_types = list(SectionType)
    assert len(all_types) == 3
    assert SectionType.TOKENIZED in all_types
    assert SectionType.STATIC in all_types
    assert SectionType.RUNTIME in all_types


def test_section_type_from_string():
    """Test creating SectionType from string value."""
    assert SectionType("tokenized") == SectionType.TOKENIZED
    assert SectionType("static") == SectionType.STATIC
    assert SectionType("runtime") == SectionType.RUNTIME


def test_section_type_invalid_string_raises_error():
    """Test that invalid string raises ValueError."""
    with pytest.raises(ValueError):
        SectionType("invalid_type")
