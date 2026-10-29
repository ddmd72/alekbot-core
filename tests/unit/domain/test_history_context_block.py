"""The text one `*_context` key adds to full_text: the drive's key as history lines, every other as compact JSON,
nothing for a non-context key or an empty value. Invented items only."""
import pytest

from src.domain.history_context_block import history_context_block

LINE_ENTRY = {"path": "Home/Lisbon lease.pdf", "ref": "drive:def456", "op": "opened"}


def test_the_drive_key_renders_lines():
    assert history_context_block("drive_context", [[LINE_ENTRY, LINE_ENTRY]]) == (
        "[Drive: Home/Lisbon lease.pdf ref=drive:def456 — opened whole]")


def test_other_context_keys_keep_their_compact_json():
    assert history_context_block("web_search_context", [{"query": "lab report units", "result": "mmol/L"}]) == (
        '{"web_search_context":[{"query":"lab report units","result":"mmol/L"}]}')


@pytest.mark.parametrize("key, value", [("drive_status", "ok"), ("drive_context", []), ("drive_context", None),
                                        ("drive_context", [[{}]]), ("web_search_context", {})])
def test_nothing_to_render_is_empty(key, value):
    assert history_context_block(key, value) == ""
