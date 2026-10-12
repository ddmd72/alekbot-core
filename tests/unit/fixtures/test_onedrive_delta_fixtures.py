"""The OneDrive delta wire fixtures are complete and PII-free (USER_DRIVE_INDEXING_RFC §17.3).

They are built from the owner's spike recordings by scripts/onedrive/sanitize_delta_fixtures.py,
which also checks every original value against its output; these checks guard later hand edits."""
import json
import re
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "onedrive_delta"
_FILES = ("approot.json", "initial_enumeration.json", "folder_delete.json", "delete_restore.json",
          "move_out_in.json", "reemitted.json", "facets.json", "resync_410.json")


def _text(name):
    return (_DIR / name).read_text(encoding="utf-8")


def test_every_scenario_is_present():
    assert sorted(p.name for p in _DIR.glob("*.json")) == sorted(_FILES)


@pytest.mark.parametrize("name", _FILES)
def test_no_real_ids_emails_or_non_ascii_names(name):
    raw = _text(name)
    assert "!s" not in raw                                   # real OneDrive item ids look like <drive>!s<hex>
    assert not re.search(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}", raw)
    assert raw.isascii()                                      # every name is file-NNNN / folder-NNNN


@pytest.mark.parametrize("name", _FILES[:-1])
def test_names_are_synthetic(name):
    names = set(re.findall(r'"name": "([^"]*)"', _text(name)))
    allowed = re.compile(r"^(file-\d{4}(\.\w+)?|folder-\d{4}|Alek-bot|appRoot)$")
    assert all(allowed.match(n) for n in names), names


def test_paths_and_urls_are_synthetic():
    for name in _FILES:
        data = _text(name)
        for path in re.findall(r'"path": "([^"]*)"', data):
            assert re.fullmatch(r"/drive/root:(/(folder-\d{4}|Alek-bot))*", path), path
        for url in re.findall(r'"webUrl": "([^"]*)"', data):
            assert url.startswith("https://onedrive.example.invalid/item/"), url


@pytest.mark.parametrize("name", [n for n in _FILES if n not in ("approot.json", "resync_410.json")])
def test_every_recorded_fixture_names_its_source_report(name):
    source = json.loads(_text(name))["_meta"]["source"]
    stamps = source.values() if isinstance(source, dict) else [source]
    assert all(re.fullmatch(r"\d{14}", s) for s in stamps), source


def test_sanitiser_drops_keys_outside_the_allowlist():
    from scripts.onedrive.sanitize_delta_fixtures import Sanitizer
    san = Sanitizer("ABCDEF0000000009", "App")
    out = san.clean({"id": "ABCDEF0000000009!x1", "description": "private note", "name": "Lease.pdf",
                     "photo": {"takenDateTime": "2020-01-01T00:00:00Z", "unknownExif": "x"}})
    assert set(out) == {"id", "name", "photo"} and set(out["photo"]) == {"takenDateTime"}
    assert san.dropped == {"description", "unknownExif"}


def test_sanitiser_check_catches_a_short_original_name():
    from scripts.onedrive.sanitize_delta_fixtures import Sanitizer, _check
    san = Sanitizer("ABCDEF0000000009", "App")
    san.name("ab", folder=False)                       # below the substring check's length floor
    with pytest.raises(SystemExit):
        _check({"pages": [{"value": [{"name": "ab"}]}]}, san)


def test_the_410_fixture_is_marked_synthetic():
    data = json.loads(_text("resync_410.json"))
    assert data["_meta"]["scenario"].startswith("SYNTHETIC")
    assert data["status"] == 410 and data["headers"]["Location"].startswith("https://graph.microsoft.com/")
