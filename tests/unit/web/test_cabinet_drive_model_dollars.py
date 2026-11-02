"""The Cabinet shows Search v2's model dollars (USER_DRIVE_INDEXING_RFC §25.4.5; review rv_sv2_p5 MINOR 3): this
month's dollars per class against its cap and the initial build's progress ("finished" once it closed), in place of
the retired §24.7 page and image counters. Since the drive card redesign both are meters: a label, "spent of cap" and a
bar."""
import json
import re
import shutil

import pytest

from src.domain.drive_enrichment import USD_CAPS, UsageClass
from src.domain.drive_index_params import INITIAL_BUILD_USD
from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts
from tests.unit.web.test_cabinet_drive_redesign import _card, _render, _row
from tests.unit.web.test_user_cabinet_drive_index_card import _indexing

KEYS = ("drive.spend_title", "drive.spent_of", "drive.finished_spent", "drive.initial_build_label",
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
    # one meter text serves a class against its cap and the open build against its allowance
    assert all(p in dicts[lang]["drive.spent_of"] for p in ("{spent}", "{cap}"))
    assert "{spent}" in dicts[lang]["drive.finished_spent"]
    if lang != "en":
        assert [k for k in KEYS if dicts[lang][k] == dicts["en"][k]] == []


def test_the_card_renders_the_dollars_and_no_page_counters():
    html, _ = _dicts()
    render = re.search(r"function renderDriveIndexing\(ix, linked\) \{(.*?)\n      \}", html, re.S).group(1)
    for field in ("ix.model_usd_month", "ix.model_usd_caps", "ix.initial_build"):
        assert field in render, field
    assert "model_pages" not in render and "triage_images" not in render


_METER_TEXTS = ('t = (key) => key === "drive.spent_of" ? ' + json.dumps("{spent} of {cap}")
                + ' : key === "drive.finished_spent" ? ' + json.dumps("finished, {spent}") + ' : "T:" + key;')


def _meters(month: dict, caps: dict, build: dict) -> str:
    row = _row(means="area", counters={"carded": 5})
    row["indexing"].update(model_usd_month=month, model_usd_caps=caps, initial_build=build)
    return _card(_render([row], 'driveUiOf("c1").tab = "index";' + _METER_TEXTS), "c1")


def _meter(html: str, label: str):
    found = re.search(rf'<div class="dv-meter"><span>T:{re.escape(label)}</span><span class="v">(.*?)</span>', html)
    return found and found.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_meters_show_each_class_against_its_cap_and_the_initial_build():
    html = _meters({"reader": 0.12345, "profile": 1.5}, {"reader": 5.0, "profile": 10.0},
                   {"spent_usd": 12.5, "allowance_usd": 40.0, "closed": False})
    assert "T:drive.spend_title" in html
    assert _meter(html, "drive.index_usd_reader") == "$0.12 of $5.00"
    assert _meter(html, "drive.index_usd_profile") == "$1.50 of $10.00"
    assert not _meter(html, "drive.index_usd_triage")            # neither spent nor capped: no meter
    assert _meter(html, "drive.initial_build_label") == "$12.50 of $40.00"
    closed = _meters({}, {}, {"spent_usd": 38.2, "allowance_usd": 40.0, "closed": True})
    assert _meter(closed, "drive.initial_build_label") == "finished, $38.20"
    assert "T:drive.spend_title" not in closed                   # nothing spent or capped this month
    nothing = _meters({}, {}, {})
    assert "dv-meter" not in nothing


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
