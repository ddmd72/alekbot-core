"""EmailSearchAgent result JSON: `until` and `listed` on the fresh list, `listed` per key, notes as kinds."""
import json
from datetime import datetime, timezone

from src.domain.exceptions import EmailNotConnected
from tests.unit.agents.test_email_search_agent_fresh_headers import FakeService, _agent, _hit, _meta, _search

UTC = timezone.utc
WATERMARK = datetime(2026, 3, 4, 6, 0, tzinfo=UTC)


async def test_until_is_the_local_end_of_the_requested_period_when_before_was_set():
    service = FakeService(watermark=datetime(2026, 2, 20, 6, 0, tzinfo=UTC))
    service.listing[None] = ([_meta("m1")], False)
    _, body = await _search(_agent(service, {"date_to": "2026-03-02"}, tz="America/New_York"))
    assert body["fresh"]["until"] == "2026-03-02 23:59"


async def test_until_is_null_without_a_past_end():
    service = FakeService(watermark=WATERMARK)
    service.listing[None] = ([_meta("m1")], False)
    _, body = await _search(_agent(service, {}))
    assert body["fresh"]["until"] is None


async def test_listed_counts_before_the_dedupe():
    service = FakeService(index=[_hit("e1")], watermark=WATERMARK)
    service.listing[None] = ([_meta("e1"), _meta("m1")], False)
    service.listing["FA-0137"] = ([_meta("e1")], False)
    service.listing["HW-55812"] = ([], False)
    _, body = await _search(_agent(service, {"exact_keys": ["FA-0137", "HW-55812"]}))
    assert body["fresh"]["listed"] == 2 and len(body["fresh"]["emails"]) == 1
    assert [(g["key"], g["listed"], len(g["emails"])) for g in body["by_key"]] == [
        ("FA-0137", 1, 0), ("HW-55812", 0, 0)]


async def test_a_fully_deduped_fresh_list_keeps_listed_above_zero():
    service = FakeService(index=[_hit("e1")], watermark=WATERMARK)
    service.listing[None] = ([_meta("e1")], False)
    _, body = await _search(_agent(service, {}))
    assert body["fresh"]["listed"] == 1 and body["fresh"]["emails"] == []


async def test_notes_hold_kinds_not_texts():
    service = FakeService(index=[_hit("e1")], watermark=None)
    service.listing[None] = RuntimeError("boom")
    service.listing["FA-0137"] = EmailNotConnected("u")
    response, body = await _search(_agent(service, {"exact_keys": ["FA-0137"]}))
    assert body["notes"] == ["unavailable", "not_connected"]
    assert json.loads(response.result)["notes"] == body["notes"]
