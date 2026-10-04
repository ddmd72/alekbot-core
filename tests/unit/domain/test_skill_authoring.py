"""Tests for skill authoring constants and catalog offering line."""

from src.domain.skill import Skill, _CATALOG_HEADER, render_catalog, save_command, skill_saved_note


def test_save_command():
    assert save_command("7f3a") == "$skill save 7f3a"


def test_saved_note():
    assert skill_saved_note("flight-status", 3) == '[System: skill "flight-status" v3 saved by the owner]'


def test_catalog_carries_the_offering_line():
    text = render_catalog([Skill(name="a", description="Use when a.", body="b")])
    assert "offer to save it as a skill" in text


def test_catalog_header_lines_are_properly_formatted():
    lines = _CATALOG_HEADER.split("\n")
    # Every line must start with the comment marker
    for line in lines:
        assert line.startswith("    // "), f"Line does not start with comment marker: {line!r}"
    # Offering text spans exactly two lines
    offering_lines = [l for l in lines if "offer to save it as a skill" in l or "procedure you will need again" in l]
    assert len(offering_lines) == 2, f"Expected 2 offering lines, got {len(offering_lines)}: {offering_lines}"
