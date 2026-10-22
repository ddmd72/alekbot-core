"""The Cabinet drive card for Slice 2+4 (USER_DRIVE_INDEXING_RFC §6, §24.7, §17.7): the API returns the readable
chunks; the card shows them in en/uk/fr/es (the model usage moved to dollars, §25.4.5, review rv_sv2_p5 MINOR 3:
its API, strings and render are pinned in test_cabinet_drive_model_dollars.py); the disclosure names the
model providers that read scans, images and hard PDFs, and says every picture is sent as a small preview."""
import pytest

from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

NEW_KEYS = ("drive.index_chunks", "drive.index_model_usage")
_PLACEHOLDERS = ("{classes}",)   # §25.4.5: the per-class dollars line (was pages and images against their budgets)
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
    assert [p for p in _PLACEHOLDERS if p not in dicts[lang]["drive.index_model_usage"]] == []


def test_translations_are_not_copies_of_english():
    _, dicts = _dicts()
    for lang in ("uk", "fr", "es"):
        assert [k for k in NEW_KEYS if dicts[lang][k] == dicts["en"][k]] == [], lang


@pytest.mark.parametrize("lang", LANGS)
def test_the_disclosure_names_the_model_providers_and_the_previews(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    assert "Anthropic" in text and "OpenAI" in text and _PREVIEW[lang] in text


def test_the_card_renders_chunks_and_usage():
    import re
    html, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    assert 't("drive.index_chunks")' in render and "ix.readable_chunks" in render
    assert 't("drive.index_model_usage")' in render
    for field in ("model_usd_month", "model_usd_caps", "initial_build"):
        assert f"ix.{field}" in render, field
