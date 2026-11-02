"""The drive card lists the files the index could not read (USER_DRIVE_INDEXING_RFC §6;
decisions/drive_unreadable_files_cabinet_only.md): the API block, the strings in every language, the card's rendering
(links only for https, the reason with a fallback, "+N more") and the counters: "not read yet" shown, `unread` not."""
import re

import pytest

from src.domain.drive_index_params import CABINET_UNREADABLE_MAX
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.drive_unreadable_file import UnreadableFile
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

REASONS = ("no_text", "machine_text", "too_large", "user_chunk_cap", "failed")
KEYS = ("drive.index_unreadable_title", "drive.index_unreadable_more",
        *(f"drive.unreadable_reason.{r}" for r in REASONS))


async def test_the_api_returns_the_files_and_the_total():
    files = (UnreadableFile("scan_0012.pdf", "no_text", "https://example.test/a"),
             UnreadableFile("scan_0013.pdf", "failed", ""))
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={"unread": 7}, unreadable=files,
                                                unreadable_total=7))
    assert block["unreadable"] == {
        "files": [{"name": "scan_0012.pdf", "reason": "no_text", "web_url": "https://example.test/a"},
                  {"name": "scan_0013.pdf", "reason": "failed", "web_url": ""}],
        "total": 7}


async def test_the_api_returns_an_empty_block_by_default():
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={}))
    assert block["unreadable"] == {"files": [], "total": 0}


@pytest.mark.parametrize("lang", LANGS)
def test_every_string_exists_in_every_language(lang):
    _, dicts = _dicts()
    assert [k for k in KEYS if not dicts[lang].get(k)] == []
    assert "{n}" in dicts[lang]["drive.index_unreadable_more"]
    if lang != "en":
        assert [k for k in KEYS if dicts[lang][k] == dicts["en"][k]] == []


def _render():
    html, _ = _dicts()
    return re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)


def test_the_block_appears_only_for_a_positive_total_and_lists_the_rest_as_more():
    render = _render()
    assert "num(unreadable.total) > 0" in render
    assert "num(unreadable.total) - unreadableFiles.length" in render and "drive.index_unreadable_more" in render
    assert render.index("drive.index_unreadable_title") < render.index("drive.index_unreadable_more")


def test_a_name_is_escaped_and_linked_only_for_https():
    render = _render()
    assert 'startsWith("https://")' in render
    assert "escapeHtml(f.web_url)" in render and "escapeHtml(f.name)" in render
    assert 'target="_blank" rel="noopener"' in render


def test_an_unknown_reason_reads_as_failed():
    render = _render()
    assert '"drive.unreadable_reason." + (known ? f.reason : "failed")' in render
    assert all(f'"{r}"' in render for r in REASONS)


def test_not_read_yet_is_shown_and_the_combined_unread_counter_is_not():
    html, dicts = _dicts()
    labels = dict(re.findall(r'(\w+): "([\w.]+)"', re.search(r"const DRIVE_COUNTER_LABELS = \{(.*?)\};", html, re.S).group(1)))
    assert labels["not_read_yet"] == "drive.index_unread" and "unread" not in labels
    render = _render()
    assert 'key === "unread"' in render and 'key === "not_read_yet"' not in render


def test_the_list_is_short():
    assert CABINET_UNREADABLE_MAX == 5
