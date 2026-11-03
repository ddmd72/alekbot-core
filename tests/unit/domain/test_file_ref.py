"""The ref classifier (EXTERNAL_FILES_RFC §4.1, P9): the store a ref names, in a fixed order. A prefix decides before
the drive codec (which finds `drive:` anywhere), and `email:` decides before everything."""
import pytest

from src.domain.file_ref import EMAIL_REF_PREFIX, FileRefKind, classify_ref
from src.domain.skill import DELIVERED_REF_PREFIXES


@pytest.mark.parametrize("ref, kind", [
    ("email:msg-17/att-2", FileRefKind.EMAIL),
    ("skill:drive-search/SKILL.md", FileRefKind.SKILL),
    ("workspace:notes/Q3 budget.md", FileRefKind.WORKSPACE),
    ("docs/Porto lease summary.docx", FileRefKind.DELIVERED),
    ("drive:od1:01ABCDEF", FileRefKind.DRIVE),
    ("Porto lease.pdf", FileRefKind.CHAT),
])
def test_each_kind(ref, kind):
    assert classify_ref(ref) is kind


@pytest.mark.parametrize("prefix", DELIVERED_REF_PREFIXES)
def test_every_delivered_prefix_is_delivered(prefix):
    assert classify_ref(f"{prefix}Tom's plan.docx") is FileRefKind.DELIVERED


@pytest.mark.parametrize("ref, kind", [
    ("skill:notes/drive:od1:01ABC.md", FileRefKind.SKILL),
    ("workspace:drive:od1:01ABC.md", FileRefKind.WORKSPACE),
    ("docs/drive:od1:01ABC.pdf", FileRefKind.DELIVERED),
    ("email:msg-17/drive:od1:01ABC.pdf", FileRefKind.EMAIL),
    ("email:skill:x", FileRefKind.EMAIL),
])
def test_a_prefix_keeps_its_store_whatever_the_name_holds(ref, kind):
    assert classify_ref(ref) is kind


@pytest.mark.parametrize("ref", [
    "drive:od1:01ABC", "drive:01ABC", "the file (drive:gd2:1xYz) from Hanna", "Inbox/drive:od1:01ABC.pdf",
])
def test_drive_anywhere_else_is_drive(ref):
    assert classify_ref(ref) is FileRefKind.DRIVE


@pytest.mark.parametrize("ref", ["", "Porto lease.pdf", "R&D/Tom's plan.docx", "drive", "email", "skill"])
def test_a_plain_name_and_the_empty_string_are_chat(ref):
    assert classify_ref(ref) is FileRefKind.CHAT


def test_the_kinds_are_the_store_strings():
    assert {k.value for k in FileRefKind} == {"email", "skill", "workspace", "delivered", "drive", "chat"}
    assert EMAIL_REF_PREFIX == "email:"
