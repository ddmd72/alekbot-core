"""Domain rules for the user's drive (docs/10_rfcs/USER_DRIVE_RFC.md §4.3, §4.6–§4.9)."""
import pytest

from src.domain.user_drive import (
    DEFAULT_INBOX_FOLDER,
    DriveItem,
    DriveItemNotFoundError,
    DrivePathError,
    drive_context_entry,
    drive_filename,
    drive_label,
    is_drive_ref,
    is_text_file,
    join_drive_path,
    make_drive_ref,
    match_child_folder,
    name_key,
    parse_drive_ref,
    sanitize_drive_filename,
    split_folder_path,
)


def _folder(name: str, item_id: str = "f1", path: str = "") -> DriveItem:
    return DriveItem(item_id=item_id, name=name, path=path or name, is_folder=True)


class TestRefs:
    def test_round_trip(self):
        assert parse_drive_ref(make_drive_ref("ABC!123")) == "ABC!123"

    def test_non_drive_refs(self):
        for ref in ("report.docx", "docs/u1/x.pdf", "skill:a/b.md", "", "drive:"):
            assert parse_drive_ref(ref) is None
            assert is_drive_ref(ref) is False

    def test_parse_tolerates_label_punctuation(self):
        assert parse_drive_ref('"drive:abc"') == "abc"
        assert parse_drive_ref("drive:abc]") == "abc"
        assert parse_drive_ref(" ref=drive:abc ") == "abc"


class TestSplitFolderPath:
    @pytest.mark.parametrize("raw,expected", [
        ("Встречи/2026", ["Встречи", "2026"]),
        ("/Встречи/2026/ ", ["Встречи", "2026"]),
        ("Встречи//2026", ["Встречи", "2026"]),
        ("  Inbox ", ["Inbox"]),
        ("", []),
        ("/", []),
    ])
    def test_normalises(self, raw, expected):
        assert split_folder_path(raw) == expected

    @pytest.mark.parametrize("raw", ["../x", "a/../b", "a/./b", "a\\b"])
    def test_rejects_escape_and_backslash(self, raw):
        with pytest.raises(DrivePathError):
            split_folder_path(raw)


class TestMatchChildFolder:
    def test_case_insensitive(self):
        assert match_child_folder([_folder("Встречи", "f1")], "встречи").item_id == "f1"

    def test_exact_case_wins(self):
        assert match_child_folder([_folder("docs", "a"), _folder("Docs", "b")], "Docs").item_id == "b"

    def test_files_are_not_folders(self):
        kids = [DriveItem(item_id="x", name="Встречи", path="Встречи", is_folder=False)]
        assert match_child_folder(kids, "Встречи") is None

    def test_nfd_name_matches_nfc(self):
        import unicodedata
        nfd = unicodedata.normalize("NFD", "Мой отчёт")
        assert name_key(nfd) == name_key("мой отчёт")
        assert match_child_folder([_folder(nfd, "f9")], "Мой отчёт").item_id == "f9"


class TestNames:
    def test_strips_delivered_uuid_prefix(self):
        ref = "docs/u1/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b-Отчёт за май.docx"
        assert drive_filename(None, ref) == "Отчёт за май.docx"

    def test_chat_upload_keeps_name(self):
        assert drive_filename(None, "image (3).png") == "image (3).png"

    def test_requested_name_gets_source_extension(self):
        assert drive_filename("Договор аренды", "lease.pdf") == "Договор аренды.pdf"
        assert drive_filename("Договор v2.1", "lease.pdf") == "Договор v2.1.pdf"
        assert drive_filename("lease.PDF", "lease.pdf") == "lease.PDF"
        assert drive_filename("notes.md", "x.txt") == "notes.md"

    def test_forbidden_characters(self):
        assert sanitize_drive_filename('a/b:c*d?"e<f>g|h\\i.txt') == "a_b_c_d__e_f_g_h_i.txt"
        assert sanitize_drive_filename("   ...  ") == "file"

    def test_text_files(self):
        assert is_text_file("a.md", "") and is_text_file("a.bin", "text/plain")
        assert not is_text_file("a.pdf", "application/pdf")


class TestLabels:
    def test_file_label(self):
        item = DriveItem(item_id="id1", name="a.m4a", path="Встречи/a.m4a", is_folder=False, size_bytes=12_900_000)
        assert drive_label(item) == "[Drive: Встречи/a.m4a (12.3MB) ref=drive:id1]"

    def test_folder_and_root(self):
        assert drive_label(_folder("Встречи", "f1")) == "[Drive folder: Встречи/ ref=drive:f1]"
        root = DriveItem(item_id="r", name="Alek-bot", path="", is_folder=True)
        assert drive_label(root) == "[Drive folder: / ref=drive:r]"

    def test_context_entry(self):
        item = DriveItem(item_id="id1", name="a.txt", path="a.txt", is_folder=False)
        assert drive_context_entry(item) == {"path": "a.txt", "ref": "drive:id1"}


def test_join_inbox_and_not_found():
    assert join_drive_path("", "a.txt") == "a.txt"
    assert join_drive_path("Встречи", "a.txt") == "Встречи/a.txt"
    assert DEFAULT_INBOX_FOLDER == "Inbox"
    assert issubclass(DriveItemNotFoundError, FileNotFoundError)
