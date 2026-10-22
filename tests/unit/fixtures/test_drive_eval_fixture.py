"""The eval fixture is well-formed and PII-free (USER_DRIVE_INDEXING_RFC §17.8). Pure JSON checks: this
test was written before any Slice 1 code (§18)."""
import pytest
import json
import re
from pathlib import Path

pytestmark = pytest.mark.datafile

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
_CASE_TYPES = {"exact", "partial", "translit", "misspelled", "kind", "date", "place", "folder", "filter_only",
               "version_family", "semantic"}
# Per-format kinds of the policy (§5.1) and the drive-kind families of RFC Δ20.
_KINDS = {"pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "ods", "odp", "csv", "markdown", "text",
          "html", "image", "audio", "video", "archive", "code",
          "spreadsheet", "document", "presentation", "photo", "picture"}
_K = 8


def _tree():
    return json.loads((_DIR / "tree.json").read_text(encoding="utf-8"))


def _gold():
    return json.loads((_DIR / "gold_g1.json").read_text(encoding="utf-8"))


def _all_paths(tree):
    paths = {i["path"] for i in tree["items"]}
    for g in tree["generate"]:
        for n in range(g["count"]):
            paths.add(f"{g['folder']}/{g['pattern'].format(n=n)}")
    return paths


def test_every_gold_path_exists_in_the_tree():
    paths = _all_paths(_tree())
    missing = [(c["id"], p) for c in _gold() for p in c["expected_paths"] if p not in paths]
    assert missing == []


def test_gold_covers_each_case_type_three_times():
    counts = {}
    for c in _gold():
        counts[c["case"]] = counts.get(c["case"], 0) + 1
    for case in _CASE_TYPES - {"version_family"}:
        assert counts.get(case, 0) >= 3, case
    assert counts.get("version_family", 0) >= 1
    assert len(_gold()) >= 36


def test_gold_cases_are_well_formed():
    gold = _gold()
    ids = [c["id"] for c in gold]
    assert len(ids) == len(set(ids))
    for c in gold:
        assert c["case"] in _CASE_TYPES, c["id"]
        assert c["goal"] == "G1" and c["lang"] in {"en", "ru", "uk", "es", "fr"}, c["id"]
        assert c["expected_paths"] and len(set(c["expected_paths"])) == len(c["expected_paths"]), c["id"]
        assert set(c["filters"]) <= {"kind", "date_from", "date_to", "folder"}, c["id"]
        assert c["filters"].get("kind") in _KINDS | {None}, c["id"]
        assert 1 <= c.get("k", _K) <= _K, c["id"]
        assert c["query"] or c["filters"], c["id"]


def test_parents_exist_for_every_item():
    tree = _tree()
    folders = {i["path"] for i in tree["items"] if i["folder"]} | {g["folder"] for g in tree["generate"]}
    for path in _all_paths(tree):
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        assert parent == "" or parent in folders, path


def test_scale_cases_are_present():
    tree = _tree()
    by_folder = {g["folder"]: g["count"] for g in tree["generate"]}
    assert by_folder["Archive/Camera roll"] >= 1000 and by_folder["Vault"] == 800
    assert by_folder["Archive/Invoices 2024"] == 600 and by_folder["Dumps/site-mirror"] == 700
    exact = {c["query"]: c for c in _gold() if c["case"] == "exact"}
    assert exact["IMG_4912"].get("k") == 1


def test_no_email_addresses_or_ids():
    raw = (_DIR / "tree.json").read_text(encoding="utf-8") + (_DIR / "gold_g1.json").read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw)
    assert "!s" not in raw   # no real OneDrive item ids
