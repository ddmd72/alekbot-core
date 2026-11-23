"""The Cabinet's Index Gmail modal offers only incremental and backfill; the re-index mode is refused by the route."""
import re

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()


def _update_index_modal() -> str:
    start = _HTML.index("function updateIndexModal()")
    end = _HTML.index("async function submitIndexModal()")
    return _HTML[start:end]


def test_no_reindex_radio_and_no_reindex_label():
    assert 'value="reindex"' not in _HTML
    assert "Re-index Everything" not in _HTML


def test_incremental_and_backfill_radios_are_present():
    assert re.search(r'name="index-mode"\s+value="incremental"', _HTML)
    assert re.search(r'name="index-mode"\s+value="backfill"', _HTML)


def test_update_index_modal_does_not_name_reindex():
    assert "reindex" not in _update_index_modal()
