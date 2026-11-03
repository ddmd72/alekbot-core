"""Cabinet drive card text (USER_DRIVE_INDEXING_RFC §6): every new string in en/uk/fr/es, a label for every
counter the index writes ("records per state") plus the labelled enrichment counters, every labelled counter rendered
and nothing else (no raw-key fallback), the reconnect hint matched to whether search runs, and no index switch offered
on a deployment where indexing cannot run. The behavioural checks run the card under node, over the harness of
test_cabinet_drive_redesign."""
import re
import shutil

import pytest

from src.domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_NO_PASSAGES
from src.domain.drive_enrichment import JobState
from src.domain.drive_index import RecordState
from src.domain.drive_search import (
    INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER, NOT_READ_YET_COUNTER, UNREAD_COUNTER,
)
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_cabinet_drive_redesign import _card, _render, _row

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
# The enrichment counters the card labels (owner decision): the described and content-readable files. The other
# `profile_*`, `passages_*` and `coverage_*` stats stay out.
ENRICHMENT_LABELLED = {f"profile_{JobState.CURRENT.value}", f"passages_{JobState.CURRENT.value}"}
# Written to the sync state and read by search, never labelled (owner decision 2026-11-03): sheets, books and data
# files get no passage hints by policy, so their count is no problem for the owner.
DELIBERATELY_UNLABELLED = {COVERAGE_COUNTERS[GAP_NO_PASSAGES]}

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
    assert set(labels) == ({s.value for s in RecordState} | {"carded", NOT_READ_YET_COUNTER, INACCESSIBLE_COUNTER}
                           | ENRICHMENT_LABELLED)
    assert not DELIBERATELY_UNLABELLED & set(labels)
    assert [(lang, k) for lang in LANGS for k in labels.values() if not dicts[lang].get(k)] == []


def _all_counters(counters: dict, **over) -> str:
    """The "All counters" list of the Search index tab, for a row with these counters."""
    row = _row(means="area", counters=counters)
    row["indexing"].update(over)
    html = _card(_render([row], 'driveUiOf("c1").tab = "index";'), "c1")
    return html.split("T:drive.all_counters")[1].split("</details>")[0]


@node
def test_every_non_zero_counter_is_rendered():
    html, _ = _dicts()
    labels = _counter_labels(html)
    # a distinct non-zero value per labelled counter: each comes back on its own labelled line
    counters = {key: 101 + n for n, key in enumerate(labels)}
    rows = dict(re.findall(r"<dt[^>]*>T:(drive\.\w+)</dt><dd[^>]*>([\d,]+)</dd>", _all_counters(counters)))
    assert {key: rows.get(label) for key, label in labels.items()} == {key: str(v) for key, v in counters.items()}


@node
def test_the_reconnect_hint_follows_whether_search_keeps_an_index():
    # MULTI_DRIVE_RFC §11.7: the hint is per connection, in the reconnect banner of its card
    def banner(enabled: bool, kept: bool) -> str:
        row = _row(means="area", enabled=enabled, reconnect={"credential_id": "k"})
        row["indexing"]["index_kept"] = kept
        return _card(_render([row]), "c1")
    assert "T:drive.reconnect_hint<" in banner(True, True) and "T:drive.reconnect_hint_neutral" not in banner(True, True)
    for enabled, kept in ((True, False), (False, True), (False, False)):
        html = banner(enabled, kept)
        assert "T:drive.reconnect_hint_neutral" in html and "T:drive.reconnect_hint<" not in html, (enabled, kept)


@node
def test_no_switch_is_offered_where_indexing_cannot_run():
    # MULTI_DRIVE_RFC §11.7: each connection offers its own switch (the Search index tab), under the same rule
    def card(available: bool, enabled: bool) -> str:
        row = _row(means="area", enabled=enabled)
        row["indexing"]["available"] = available
        return _card(_render([row], 'driveUiOf("c1").tab = "index";'), "c1")
    for available, enabled in ((True, False), (False, True), (True, True)):
        html = card(available, enabled)
        assert 'id="drive-index-toggle-c1"' in html and 'id="drive-tab-c1-index"' in html, (available, enabled)
    html = card(False, False)
    assert "drive-index-toggle-" not in html and "drive-tab-c1-index" not in html
    assert 'id="drive-tab-c1-overview" aria-selected="true"' in html          # the stored tab falls back
    row = _row(means="area", enabled=False, indexing=None)                     # no index block at all
    html = _card(_render([row], 'driveUiOf("c1").tab = "index";'), "c1")
    assert "drive-index-toggle-" not in html and "drive-tab-c1-index" not in html


@node
def test_the_inaccessible_label_is_translated_and_the_notice_gate_is_never_shown():
    """§26.2: the `inaccessible` counter reads in the user's language, not as a raw key or an English copy; the
    fan-out's gate counter is never shown, like `unread` (no counter is shown without a label)."""
    html, dicts = _dicts()
    labels = _counter_labels(html)
    key = labels[INACCESSIBLE_COUNTER]
    for lang in ("uk", "fr", "es"):
        assert dicts[lang][key] != dicts["en"][key], lang
    assert INACCESSIBLE_UNNOTIFIED_COUNTER not in labels and UNREAD_COUNTER not in labels
    more = _all_counters({INACCESSIBLE_COUNTER: 3, INACCESSIBLE_UNNOTIFIED_COUNTER: 7771, UNREAD_COUNTER: 8881})
    assert f"T:{key}</dt>" in more
    assert "7771" not in more and "7,771" not in more and "8881" not in more and "8,881" not in more
    assert INACCESSIBLE_UNNOTIFIED_COUNTER not in more and f">{UNREAD_COUNTER}<" not in more
