"""The Search v2 eval fixture is what its families say (USER_DRIVE_INDEXING_RFC §25.7):
- the overlay names only files in files_v2/ (or the shared pictures), every one exists, and make_fixtures_v2 makes
  exactly them, byte for byte (the enrichment snapshot is keyed by these bytes);
- gold_v2.json is valid against the merged drive, covers every family of §25.7 and carries the cases the RFC names
  (the Russian cholesterol query over a Spanish lab report, all my receipts, what expires, the incident shape);
- bars_v2.json holds §25.7's numbers;
- the long book mentions each tempting word once, far apart; nothing in the overlay's text is an e-mail address
  outside example.org."""
import json
import re
from pathlib import Path

from scripts.validation.drive_eval import fixture_texts_v2
from scripts.validation.drive_eval.make_fixtures_v2 import FIXTURES_V2
from scripts.validation.drive_eval.search_v2_eval import v2_drive
from scripts.validation.drive_eval.v2_metrics import BOOK, FAMILIES, validate_gold_v2

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"


def _overlay():
    return json.loads((_DIR / "tree_v2.json").read_text(encoding="utf-8"))


def test_the_overlay_names_exactly_the_generated_files_and_they_match_byte_for_byte():
    named = {i["content"] for i in _overlay()["items"] if i.get("content")}
    assert named == {f"files_v2/{n}" for n in FIXTURES_V2}
    on_disk = {f"files_v2/{p.name}" for p in (_DIR / "files_v2").iterdir() if p.is_file()}
    assert on_disk == named
    for name, build in FIXTURES_V2.items():
        assert (_DIR / "files_v2" / name).read_bytes() == build(), name


def test_overlay_thumbnails_are_the_shared_pictures():
    for item in _overlay()["items"]:
        if item.get("thumbnail"):
            assert (_DIR / item["thumbnail"]).exists()


def test_gold_v2_is_valid_and_covers_every_family():
    gold = json.loads((_DIR / "gold_v2.json").read_text(encoding="utf-8"))
    validate_gold_v2(gold, v2_drive(_DIR / "tree.json", _DIR / "tree_v2.json").paths())
    assert {c["family"] for c in gold} == set(FAMILIES)
    queries = {c["query"] for c in gold}
    for q in ("медицинские записи по холестерину", "all my receipts", "what expires", "everything about Rome"):
        assert q in queries
    incident = next(c for c in gold if c["id"] == "inc-rome")
    assert len(incident["expected_paths"]) > 10        # more than ABOUT_SHOWN
    assert any(c.get("expect_absent") for c in gold if c["family"] == "negative")


def test_bars_v2_are_the_rfc_numbers():
    bars = json.loads((_DIR / "bars_v2.json").read_text(encoding="utf-8"))
    assert bars["pass_rate"] == {"exact": 0.95, "semantic": 0.85, "misspelling": 1.0, "negative": 0.9,
                                 "incident": 1.0}
    assert bars["file_recall"] == {"class": 0.85}
    assert (bars["exact_junk_per_case_max"], bars["automatic_junk_per_case_max"], bars["related_per_negative_max"],
            bars["book_in_exact_max"], bars["chars_median_max"], bars["chars_max"]) == (0.3, 0.5, 1.0, 0, 3000, 16000)


def test_the_book_mentions_each_tempting_word_once_and_far_apart():
    text = "\n".join(fixture_texts_v2.book_paragraphs())
    assert len(text) > 25_000 and BOOK.endswith(".docx")
    positions = []
    for word in ("Rome", "tickets", "car ", "insurance", "repair", "receipts"):
        hits = [m.start() for m in re.finditer(re.escape(word), text)]
        assert len(hits) == 1, word
        positions.append(hits[0])
    positions.sort()
    assert min(b - a for a, b in zip(positions, positions[1:])) > 2_000


def test_only_reserved_example_addresses():
    for path in (_DIR / "files_v2").iterdir():
        if path.suffix in {".md", ".txt"}:
            found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", path.read_text(encoding="utf-8"))
            assert all(a.endswith("@example.org") for a in found), path.name
