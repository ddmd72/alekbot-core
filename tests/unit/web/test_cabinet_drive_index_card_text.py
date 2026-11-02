"""Cabinet drive card text (USER_DRIVE_INDEXING_RFC §6): every new string in en/uk/fr/es, a label for every
counter the index writes ("records per state"), the reconnect hint matched to whether search runs, and no switch
offered on a deployment where indexing cannot run."""
import re

import pytest

from src.domain.drive_index import RecordState
from src.domain.drive_search import (
    INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER, NOT_READ_YET_COUNTER, UNREAD_COUNTER,
)
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts

NEW_KEYS = ("drive.reconnect_hint_neutral", "drive.index_indexed", "drive.index_metadata_only",
            "drive.index_unresolved", "drive.index_deleting", "drive.index_unread")


def _counter_labels(html):
    body = re.search(r"const DRIVE_COUNTER_LABELS = \{(.*?)\};", html, re.S).group(1)
    return dict(re.findall(r'(\w+): "([\w.]+)"', body))


@pytest.mark.parametrize("lang", LANGS)
def test_every_new_key_in_every_language(lang):
    _, dicts = _dicts()
    assert [k for k in NEW_KEYS if not dicts[lang].get(k)] == []


def test_new_translations_are_not_copies_of_english():
    _, dicts = _dicts()
    for lang in ("uk", "fr", "es"):
        assert [k for k in NEW_KEYS if dicts[lang][k] == dicts["en"][k]] == [], lang


def test_every_counter_the_index_writes_has_a_label_in_every_language():
    html, dicts = _dicts()
    labels = _counter_labels(html)
    # §26.2 (Δ56): the files the bot cannot open are a counter the user sees; `inaccessible_unnotified` is the notice's
    # gate, never shown (below). The card labels `not_read_yet` (waiting); `unread` (waiting plus unreadable) is never
    # shown: the unreadable files are listed by name (decisions/drive_unreadable_files_cabinet_only.md)
    assert set(labels) == {s.value for s in RecordState} | {"carded", NOT_READ_YET_COUNTER, INACCESSIBLE_COUNTER}
    assert [(lang, k) for lang in LANGS for k in labels.values() if not dicts[lang].get(k)] == []


def test_every_non_zero_counter_is_rendered():
    html, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    assert "DRIVE_COUNTER_LABELS" in render and "Object.keys(c)" in render


def test_the_reconnect_hint_follows_whether_search_keeps_an_index():
    html, _ = _dicts()
    # MULTI_DRIVE_RFC §11.7: the hint is per connection row of the list
    render = re.search(r"function renderDriveConnection\(row, data\) \{(.*?)\n      \}", html, re.S).group(1)
    assert 't("drive.reconnect_hint")' in render and 't("drive.reconnect_hint_neutral")' in render
    assert "index_kept" in render


def test_no_switch_is_offered_where_indexing_cannot_run():
    html, _ = _dicts()
    # MULTI_DRIVE_RFC §11.7: each connection row offers its own switch, under the same rule
    render = re.search(r"function renderDriveConnection\(row, data\) \{(.*?)\n      \}", html, re.S).group(1)
    assert "ix && (ix.available || ix.enabled) ? renderDriveIndexing(ix, true)" in render


def test_the_inaccessible_label_is_translated_and_the_notice_gate_is_never_shown():
    """§26.2: the `inaccessible` counter reads in the user's language, not as a raw key or an English copy; the
    fan-out's gate counter is skipped like `unread`."""
    html, dicts = _dicts()
    key = _counter_labels(html)[INACCESSIBLE_COUNTER]
    for lang in ("uk", "fr", "es"):
        assert dicts[lang][key] != dicts["en"][key], lang
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    assert f'key === "{INACCESSIBLE_UNNOTIFIED_COUNTER}"' in render
    assert f'key === "{UNREAD_COUNTER}"' in render   # the combined counter is skipped too (unreadable files are listed)
