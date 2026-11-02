"""The Cabinet drive card for Slice 2+4 (USER_DRIVE_INDEXING_RFC §6, §24.7, §17.7): the API returns the readable
chunks; the card shows them in en/uk/fr/es (the model usage moved to dollars, §25.4.5, review rv_sv2_p5 MINOR 3, and
is shown as meters, one per class against its cap: its API, strings and render are pinned in
test_cabinet_drive_model_dollars.py); the disclosure names the model providers that read scans, images and hard PDFs,
and says every picture is sent as a small preview."""
import json
import shutil

import pytest

from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_cabinet_drive_redesign import _card, _render, _row
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

NEW_KEYS = ("drive.index_chunks", "drive.spend_title")
# §25.4.5: the per-class dollars meter (was one line of pages and images against their budgets)
_PLACEHOLDERS = ("{spent}", "{cap}")
# The providers named in every language, and the preview clause (§6: every image is triaged, §24.5).
_PREVIEW = {"en": "small preview", "uk": "мала копія", "fr": "petit aperçu", "es": "vista previa pequeña"}


async def test_the_api_returns_chunks_and_usage_with_its_budgets():
    status = DriveIndexingStatus(enabled=True, counters={}, readable_chunks=812, usd_month={"reader": 0.37},
                                 usd_caps={"reader": 4.0})
    block = await _indexing(status)
    assert block["readable_chunks"] == 812
    assert (block["model_usd_month"], block["model_usd_caps"]) == ({"reader": 0.37}, {"reader": 4.0})


@pytest.mark.parametrize("lang", LANGS)
def test_every_new_key_in_every_language_with_its_placeholders(lang):
    _, dicts = _dicts()
    assert [k for k in NEW_KEYS if not dicts[lang].get(k)] == []
    assert [p for p in _PLACEHOLDERS if p not in dicts[lang]["drive.spent_of"]] == []


def test_translations_are_not_copies_of_english():
    _, dicts = _dicts()
    for lang in ("uk", "fr", "es"):
        assert [k for k in NEW_KEYS if dicts[lang][k] == dicts["en"][k]] == [], lang


@pytest.mark.parametrize("lang", LANGS)
def test_the_disclosure_names_the_model_providers_and_the_previews(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    assert "Anthropic" in text and "OpenAI" in text and _PREVIEW[lang] in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_card_renders_chunks_and_usage():
    import re
    row = _row(means="area", counters={"carded": 5})
    row["indexing"].update(readable_chunks=812, model_usd_month={"reader": 0.37}, model_usd_caps={"reader": 4.0})
    spent_of = json.dumps("{spent} of {cap}")
    html = _card(_render([row], 'driveUiOf("c1").tab = "index";'
                         f' t = (key) => key === "drive.spent_of" ? {spent_of} : "T:" + key;'), "c1")
    more = html.split("T:drive.all_counters")[1].split("</details>")[0]
    assert dict(re.findall(r"<dt[^>]*>T:(drive\.\w+)</dt><dd[^>]*>([\d,]+)</dd>", more))["drive.index_chunks"] == "812"
    usage = html.split("T:drive.spend_title")[1]
    assert re.search(r"T:drive\.index_usd_reader</span><span class=\"v\">\$0\.37 of \$4\.00</span>", usage)
    page, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", page, re.S).group(1)
    for field in ("model_usd_month", "model_usd_caps", "initial_build", "readable_chunks"):
        assert f"ix.{field}" in render, field
