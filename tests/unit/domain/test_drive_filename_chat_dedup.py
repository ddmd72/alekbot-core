"""drive_filename() recovers the user's name from the chat store's Finder-style dedup key."""
import pytest

from src.domain.user_drive import drive_filename


@pytest.mark.parametrize("ref,expected", [
    ("adr (1).md", "adr.md"),
    ("adr (12).md", "adr.md"),
    ("Отчёт (2).pdf", "Отчёт.pdf"),
    ("notes (1)", "notes"),
    ("a (1) (1).md", "a (1).md"),
    ("adr.md", "adr.md"),
    ("adr(1).md", "adr(1).md"),
    ("adr (x).md", "adr (x).md"),
])
def test_suffix_stripped_only_when_exactly_the_chat_store_format(ref, expected):
    assert drive_filename(None, ref) == expected


def test_uuid_prefix_and_suffix_both_removed():
    ref = "0f8fad5b-d9cb-469f-a165-70867728950e-report (1).pdf"
    assert drive_filename(None, ref) == "report.pdf"


def test_explicit_name_is_untouched():
    assert drive_filename("my notes", "adr (1).md") == "my notes.md"
