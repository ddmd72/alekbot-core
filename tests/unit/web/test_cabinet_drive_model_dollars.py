"""The Cabinet shows Search v2's model dollars (USER_DRIVE_INDEXING_RFC §25.4.5; review rv_sv2_p5 MINOR 3): this
month's dollars per class against its cap and the initial build's progress, in place of the retired §24.7 page and
image counters."""
import re

import pytest

from src.domain.drive_enrichment import USD_CAPS, UsageClass
from src.domain.drive_index_params import INITIAL_BUILD_USD
from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

KEYS = ("drive.index_model_usage", "drive.index_initial_build", "drive.index_initial_build_done",
        *(f"drive.index_usd_{c.value}" for c in UsageClass))


async def test_the_api_returns_the_month_dollars_their_caps_and_the_initial_build():
    status = DriveIndexingStatus(enabled=True, counters={}, readable_chunks=3,
                                 usd_month={"reader": 0.12345, "profile": 1.5}, usd_caps=dict(USD_CAPS),
                                 initial_build_spent_usd=12.5, initial_build_allowance_usd=40.0)
    block = await _indexing(status)
    assert block["model_usd_month"] == {"reader": 0.1235, "profile": 1.5}
    assert block["model_usd_caps"] == dict(USD_CAPS)
    assert block["initial_build"] == {"spent_usd": 12.5, "allowance_usd": 40.0, "closed": False}
    assert not any(k.startswith(("model_pages", "triage_images")) for k in block)


@pytest.mark.parametrize("lang", LANGS)
def test_every_dollar_key_in_every_language_with_its_placeholders(lang):
    _, dicts = _dicts()
    assert [k for k in KEYS if not dicts[lang].get(k)] == []
    assert "{classes}" in dicts[lang]["drive.index_model_usage"]
    assert all(p in dicts[lang]["drive.index_initial_build"] for p in ("{spent}", "{allowance}"))
    assert "{spent}" in dicts[lang]["drive.index_initial_build_done"]
    if lang != "en":
        assert [k for k in KEYS if dicts[lang][k] == dicts["en"][k]] == []


def test_the_card_renders_the_dollars_and_no_page_counters():
    html, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    for field in ("ix.model_usd_month", "ix.model_usd_caps", "ix.initial_build"):
        assert field in render, field
    assert "model_pages" not in render and "triage_images" not in render


async def test_the_status_reads_the_usage_document_and_the_initial_build():
    from tests.unit.services.test_drive_search_service import World
    from src.domain.drive_index_planner import month_key
    from tests.unit.services.drive_index_fakes import legacy_account
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    now = w.clock.now
    await w.repo.add_model_usage("u", month_key(now), now.date().isoformat(), "triage", 0.75, 0.0, 3,
                                 covered_usd=0.75)
    account = legacy_account("u")   # the initial build is the provider account's (MULTI_DRIVE_RFC §10)
    await w.repo.spend_initial_build("u", account.realm, account.subject, 0.75, now)
    # XC-4: 0.5 more that no initial build covered; the card's "x / cap" is what the cap paid, not the total
    await w.repo.add_model_usage("u", month_key(now), now.date().isoformat(), "triage", 0.5, 0.0, 1, covered_usd=0.0)
    status = await w.search_svc.indexing_status("u")
    assert status.usd_month == {"reader": 0.0, "triage": 0.5, "profile": 0.0, "passage": 0.0, "transcribe": 0.0}
    assert status.usd_caps == dict(USD_CAPS)
    assert (status.initial_build_spent_usd, status.initial_build_allowance_usd, status.initial_build_closed) == (
        0.75, INITIAL_BUILD_USD, False)
