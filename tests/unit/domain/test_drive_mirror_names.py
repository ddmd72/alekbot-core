import hashlib
import unicodedata

from src.domain.drive_index_params import MIRROR_NAME_MAX, MIRROR_PATH_MARGIN
from src.domain.drive_keys import query_concepts
from src.domain.drive_mirror import archive_folder_key, ledger_key, mirror_root_doc_id
from src.domain.drive_mirror_names import (
    LEVEL_DAY, LEVEL_OTHER, LEVEL_ROOT, Sibling, archive_place, assign_names, driver_keys, fits, full_path_length,
    mirror_file_name, place, reserved_names, root_name_candidates,
)


# the connection's `max_path_chars` (MULTI_DRIVE_RFC §7.1): 400 for the OneDrive App Folder, the limit these tests were
# written against
MAX_PATH = 400


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def test_keys():
    assert ledger_key("u", "file", "d", "s1") == "u:file:" + hashlib.sha256(b"d/s1").hexdigest()[:32]
    assert mirror_root_doc_id("u", "d") == "u:d"
    k = archive_folder_key("u", "d", "2026-10-21", "A/B")
    assert k == "u:archive:" + hashlib.sha256(b"d/2026-10-21/A/B").hexdigest()[:32]


def test_root_name_candidates():
    assert root_name_candidates() == ("_Transcripts", "_Transcripts (Alek)", "_Transcripts (Alek 2)",
                                      "_Transcripts (Alek 3)", "_Transcripts (Alek 4)", "_Transcripts (Alek 5)")


def test_name_is_source_plus_md():
    assert mirror_file_name("Interview.mp4") == "Interview.mp4.md"


def test_name_cut_at_240_with_hash():
    src = "a" * 300
    out = mirror_file_name(src)
    assert len(out) == MIRROR_NAME_MAX
    assert out.endswith(f"~{_h(src)}.md")
    assert out.startswith("a" * 100)


def test_file_x_and_folder_x_md_collide_folder_wins():
    out = assign_names([Sibling("f1", "X.md"), Sibling("d1", "X.md", True)], ())
    assert out["d1"] == "X.md"
    assert out["f1"] == f"X~{_h('f1')}.md"


def test_reserved_names_win_at_the_root_and_in_a_day_folder():
    assert reserved_names(LEVEL_ROOT) == ("_README.md", "Archive", "Long paths")
    assert reserved_names(LEVEL_DAY) == ("Long paths",)
    assert reserved_names(LEVEL_OTHER) == ()
    out = assign_names([Sibling("d1", "Long paths", True), Sibling("f1", "_README.md")], reserved_names(LEVEL_ROOT))
    assert out["d1"] == f"Long paths~{_h('d1')}"
    assert out["f1"] == f"_README~{_h('f1')}.md"
    day = assign_names([Sibling("d2", "long PATHS", True)], reserved_names(LEVEL_DAY))
    assert day["d2"].startswith("long PATHS~")


def test_source_folder_named_archive_takes_the_suffix():
    out = assign_names([Sibling("d1", "Archive", True)], reserved_names(LEVEL_ROOT))
    assert out["d1"] == f"Archive~{_h('d1')}"


def test_collision_is_case_insensitive_after_nfc():
    nfd = unicodedata.normalize("NFD", "Café.md")
    out = assign_names([Sibling("a", "cafÉ.md"), Sibling("b", nfd)], ())
    assert out["a"] == "cafÉ.md"
    assert out["b"] == f"{nfd[:-3]}~{_h('b')}.md"


def test_folder_suffix_goes_at_the_end():
    out = assign_names([Sibling("d1", "X.md", True), Sibling("d2", "x.MD", True)], ())
    assert out["d1"] == "X.md"
    assert out["d2"] == f"x.MD~{_h('d2')}"


def test_suffix_is_stable_and_goes_when_the_sibling_goes():
    both = [Sibling("a", "X.md"), Sibling("b", "X.md")]
    first = assign_names(both, ())
    assert first == assign_names(list(reversed(both)), ())
    assert first["b"].startswith("X~")
    assert assign_names([Sibling("b", "X.md")], ()) == {"b": "X.md"}


def test_truncation_collision_resolves_by_source_id():
    n = mirror_file_name("z" * 300)
    out = assign_names([Sibling("s2", n), Sibling("s1", n)], ())
    assert out["s1"] == n
    assert out["s2"] != n and out["s2"].endswith(f"~{_h('s2')}.md")


def test_taken_name_from_a_foreign_item_forces_the_suffix():
    out = assign_names([Sibling("a", "Notes.md")], (), taken=("notes.md",))
    assert out["a"] == f"Notes~{_h('a')}.md"


def test_path_counted_from_the_drive_root():
    area, root = "Apps/" + "A" * 80, "_Transcripts"          # 85 + 1 + 12 prefix chars with their separators
    parts = ("x" * 300,)
    assert full_path_length(area, root, parts) == 85 + 1 + 12 + 1 + 300
    app_relative = len(root) + 1 + 300                        # what the App Folder alone would count
    assert app_relative <= 380
    assert fits("", root, parts, MAX_PATH)
    assert full_path_length(area, root, parts) > 390
    assert not fits(area, root, parts, MAX_PATH)
    assert place(parts, area_path=area, root_path=root, max_path=MAX_PATH).long_path


def test_limit_is_400_minus_margin():
    root = "r"
    name = "n" * (MAX_PATH - MIRROR_PATH_MARGIN - 2)
    assert full_path_length("", root, (name,)) == MAX_PATH - MIRROR_PATH_MARGIN
    assert fits("", root, (name,), MAX_PATH)
    assert not fits("", root, (name + "n",), MAX_PATH)


def test_long_path_placement():
    p = place(("A", "B", "c" * 400 + ".md"), area_path="Apps/X", root_path="_Transcripts", max_path=MAX_PATH)
    assert p.long_path and p.parts == ("Long paths", "c" * 400 + ".md")
    short = place(("A", "b.md"), area_path="Apps/X", root_path="_Transcripts", max_path=MAX_PATH)
    assert not short.long_path and short.parts == ("A", "b.md")


def test_archive_place_is_dated_mirror_path():
    p = archive_place("2026-10-21", ("Videos", "2024", "Interview.mp4.md"), area_path="Apps/X", root_path="_Transcripts", max_path=MAX_PATH)
    assert p.parts == ("Archive", "2026-10-21", "Videos", "2024", "Interview.mp4.md") and not p.long_path


def test_archive_long_path():
    name = "d" * 200 + ".md"
    p = archive_place("2026-10-21", ("e" * 150, name), area_path="Apps/X", root_path="_Transcripts", max_path=MAX_PATH)
    assert p.long_path and p.parts == ("Archive", "2026-10-21", "Long paths", name)


def test_driver_keys_longest_word_first_30():
    c = query_concepts("river budget extraordinarily")
    words = [x for x in c.concepts if not x.is_number]
    longest = max(words, key=lambda x: len(x.word))
    keys = driver_keys(c.concepts)
    assert keys == tuple(longest.word_keys[:30])
    assert len(keys) <= 30 and keys


def test_driver_keys_numbers_only_without_words():
    c = query_concepts("2024 1999")
    assert c.concepts and all(x.is_number for x in c.concepts)
    assert driver_keys(c.concepts)
    mixed = query_concepts("2024 budget")
    word = [x for x in mixed.concepts if not x.is_number][0]
    assert driver_keys(mixed.concepts) == tuple(word.word_keys[:30])
    assert driver_keys(()) == ()


def test_empty_parts_are_refused():
    import pytest
    with pytest.raises(ValueError):
        place((), area_path="", root_path="r", max_path=MAX_PATH)
    with pytest.raises(ValueError):
        archive_place("2026-10-21", (), area_path="", root_path="r", max_path=MAX_PATH)
