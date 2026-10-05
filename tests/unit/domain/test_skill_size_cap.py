"""The SKILL.md cap is the Firestore document bound, not a style limit (owner, 2026-10-05)."""
import pytest
from pydantic import ValidationError

from src.domain.skill import FIRESTORE_DOC_LIMIT_BYTES, MAX_SKILL_MD_BYTES, Skill, render_skill_md


def _skill(body: str) -> Skill:
    return Skill(name="big-skill", description="Use when testing size.", body=body)


def test_cap_sits_just_under_the_firestore_document_limit():
    assert FIRESTORE_DOC_LIMIT_BYTES == 1024 * 1024
    assert FIRESTORE_DOC_LIMIT_BYTES - 64 * 1024 <= MAX_SKILL_MD_BYTES < FIRESTORE_DOC_LIMIT_BYTES


def test_a_skill_over_the_old_20kb_cap_is_accepted():
    _skill("я" * 11000)  # ~22 KB in UTF-8 — rejected before this change


def test_a_skill_just_under_the_cap_is_accepted():
    overhead = len(render_skill_md(_skill("x")).encode("utf-8")) - 1
    s = _skill("x" * (MAX_SKILL_MD_BYTES - overhead))
    assert len(render_skill_md(s).encode("utf-8")) == MAX_SKILL_MD_BYTES


def test_a_skill_over_the_cap_is_rejected_with_the_real_limit():
    with pytest.raises(ValidationError, match=f"{MAX_SKILL_MD_BYTES // 1024} KB"):
        _skill("x" * (MAX_SKILL_MD_BYTES + 1))
