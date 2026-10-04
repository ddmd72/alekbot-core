"""Tests for skill authoring constants and catalog offering line."""

from src.domain.skill import Skill, render_catalog, save_command, skill_saved_note


def test_save_command():
    assert save_command("7f3a") == "$skill save 7f3a"


def test_saved_note():
    assert skill_saved_note("flight-status", 3) == '[System: skill "flight-status" v3 saved by the owner]'


def test_catalog_carries_the_offering_line():
    text = render_catalog([Skill(name="a", description="Use when a.", body="b")])
    assert "offer to save it as a skill" in text
