"""The Cabinet says a v1 index waits for its v2 rebuild (USER_DRIVE_INDEXING_RFC §25.8, D30; review XC-8): the status
carries `awaiting_rebuild`, the API returns it and the card shows it in every language instead of "building"."""
import re

import pytest

from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

pytestmark = pytest.mark.datafile


@pytest.mark.parametrize("waiting", [True, False])
async def test_the_api_returns_awaiting_rebuild(waiting):
    block = await _indexing(DriveIndexingStatus(enabled=True, counters={}, awaiting_rebuild=waiting))
    assert block["awaiting_rebuild"] is waiting


@pytest.mark.parametrize("lang", LANGS)
def test_the_awaiting_rebuild_line_exists_in_every_language(lang):
    _, dicts = _dicts()
    assert dicts[lang].get("drive.index_awaiting_rebuild")
    if lang != "en":
        assert dicts[lang]["drive.index_awaiting_rebuild"] != dicts["en"]["drive.index_awaiting_rebuild"]


def test_the_card_shows_awaiting_rebuild_before_building():
    html, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    assert render.index("ix.awaiting_rebuild") < render.index('t("drive.index_building")')
