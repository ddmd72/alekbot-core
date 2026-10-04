"""Parse SKILL.md text (YAML frontmatter + markdown body) into a domain Skill."""

import re

import yaml
from pydantic import ValidationError

from ..domain.exceptions import SkillValidationError
from ..domain.skill import Skill

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)
_ALLOWED_KEYS = {"name", "description"}


def parse_skill_md(text: str) -> Skill:
    match = _FRONTMATTER_RE.match(text.lstrip("﻿"))
    if not match:
        raise SkillValidationError("SKILL.md must start with a '---' frontmatter block closed by '---'")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise SkillValidationError(f"frontmatter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise SkillValidationError("frontmatter must be a mapping")
    unknown = set(meta) - _ALLOWED_KEYS
    if unknown:
        raise SkillValidationError(f"unsupported frontmatter keys: {sorted(unknown)}")
    try:
        return Skill(
            name=str(meta.get("name", "")),
            description=str(meta.get("description", "")),
            body=match.group(2),
        )
    except ValidationError as e:
        raise SkillValidationError(str(e)) from e
