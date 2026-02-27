"""
Unit tests for Blueprint v4 domain model.

Tests cover:
- Blueprint creation (outer_class + class_order)
- validate() passes with valid data
- validate() raises on structural violations
- Immutability (frozen dataclass)
- Hashability (can be used in sets/dicts)
"""

import pytest
from dataclasses import FrozenInstanceError

from src.domain.prompt_v3.blueprint import Blueprint


def _make_blueprint(**kwargs) -> Blueprint:
    defaults = {
        "id": "universal_agent_v1",
        "outer_class": "Alek extends Agent",
        "class_order": ["properties", "cognitive_process", "policies"],
    }
    defaults.update(kwargs)
    return Blueprint(**defaults)


def test_blueprint_creation():
    """Blueprint stores outer_class and class_order correctly."""
    bp = _make_blueprint()

    assert bp.id == "universal_agent_v1"
    assert bp.outer_class == "Alek extends Agent"
    assert bp.class_order == ["properties", "cognitive_process", "policies"]


def test_validate_passes_with_valid_blueprint():
    """validate() does not raise for a structurally valid blueprint."""
    bp = _make_blueprint()
    bp.validate()  # should not raise


def test_validate_raises_on_empty_outer_class():
    """validate() raises ValueError when outer_class is empty or whitespace."""
    with pytest.raises(ValueError, match="outer_class cannot be empty"):
        _make_blueprint(outer_class="").validate()

    with pytest.raises(ValueError, match="outer_class cannot be empty"):
        _make_blueprint(outer_class="   ").validate()


def test_validate_raises_on_empty_class_order():
    """validate() raises ValueError when class_order is empty."""
    with pytest.raises(ValueError, match="class_order cannot be empty"):
        _make_blueprint(class_order=[]).validate()


def test_validate_raises_on_duplicate_class_order():
    """validate() raises ValueError when class_order contains duplicates."""
    with pytest.raises(ValueError, match="class_order has duplicates"):
        _make_blueprint(class_order=["properties", "cognitive_process", "properties"]).validate()


def test_blueprint_immutability():
    """Blueprint is a frozen dataclass — fields cannot be reassigned."""
    bp = _make_blueprint()

    with pytest.raises(FrozenInstanceError):
        bp.id = "new_id"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        bp.outer_class = "NewClass"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        bp.class_order = []  # type: ignore


def test_blueprint_hashable():
    """Blueprint can be used as a dict key and in sets."""
    bp1 = _make_blueprint(id="bp1")
    bp2 = _make_blueprint(id="bp2", outer_class="Other extends Agent")

    # Can use in set
    s = {bp1, bp2}
    assert len(s) == 2

    # Can use as dict key
    d = {bp1: "first", bp2: "second"}
    assert d[bp1] == "first"


def test_blueprint_equal_when_same_fields():
    """Two blueprints with identical fields are equal."""
    bp1 = _make_blueprint()
    bp2 = _make_blueprint()
    assert bp1 == bp2


def test_blueprint_class_order_preserved():
    """class_order preserves insertion order (determines section rendering order)."""
    order = ["properties", "cognitive_process", "protocols", "output_format", "policies"]
    bp = _make_blueprint(class_order=order)
    assert bp.class_order == order
