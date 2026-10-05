import pytest

from src.domain.exceptions import SkillValidationError
from src.domain.skill import Skill, render_skill_md
from src.utils.skill_md import parse_skill_md


def test_parses_plain_frontmatter():
    s = parse_skill_md("---\nname: flight-status\ndescription: Use when a flight is asked about.\n---\n1. Open.\n")
    assert s == Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.")


def test_parses_folded_description():
    text = "---\nname: a\ndescription: >\n  Use when\n  folded.\n---\nbody\n"
    assert parse_skill_md(text).description == "Use when folded."


def test_round_trips_render():
    s = Skill(name="a", description='Use when "x": y', body="line 1\nline 2")
    assert parse_skill_md(render_skill_md(s)) == s


@pytest.mark.parametrize("text", [
    "no frontmatter",
    "---\nname: a\n",                                   # unterminated
    "---\n- a\n- b\n---\nbody",                         # not a mapping
    "---\nname: a\ndescription: d\nscripts: []\n---\nb",  # unsupported key
    "---\nname: [unclosed\n---\nb",                     # invalid YAML
    "---\nname: Bad_Name\ndescription: d\n---\nb",      # fails Skill validation
])
def test_invalid_inputs_raise_skill_validation_error(text):
    with pytest.raises(SkillValidationError):
        parse_skill_md(text)
