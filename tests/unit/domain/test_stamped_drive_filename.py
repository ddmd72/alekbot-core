"""stamped_drive_filename: `<stamp> <subject><ext>`, drive-safe, capped in UTF-8 bytes."""
from src.domain.user_drive import MAX_STAMPED_NAME_BYTES, stamped_drive_filename

STAMP = "2025-08-10 14.05"


def test_the_name_is_stamp_subject_extension():
    assert stamped_drive_filename(STAMP, "Check-in at Albergo Immaginario", ".md") == "2025-08-10 14.05 Check-in at Albergo Immaginario.md"


def test_forbidden_characters_are_replaced():
    name = stamped_drive_filename(STAMP, 'Check-in/Albergo: "R&D"?', ".md")
    assert not any(ch in name for ch in '/\\:*?"<>|')
    assert name.startswith(STAMP + " Check-in") and name.endswith(".md")


def test_a_long_cyrillic_subject_is_cut_on_a_character_boundary_within_the_cap():
    name = stamped_drive_filename(STAMP, "Заселення в готель " * 40, ".md")
    assert len(name.encode("utf-8")) <= MAX_STAMPED_NAME_BYTES
    assert name.endswith(".md")
    name.encode("utf-8").decode("utf-8")   # no split character
    assert "�" not in name


def test_a_trailing_space_or_dot_left_by_the_cut_is_removed():
    name = stamped_drive_filename(STAMP, "a" * 140 + " .  more", ".md")
    stem = name[:-len(".md")]
    assert stem == stem.rstrip(". ")
    assert len(name.encode("utf-8")) <= MAX_STAMPED_NAME_BYTES
