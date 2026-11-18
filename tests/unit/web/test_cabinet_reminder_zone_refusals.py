"""Cabinet reminder routes and the user's stored zone (P16-D5): beyond the pins in
`test_cabinet_reminder_time_instants.py`. The zone is read only to place a naive `due`; a refusal names its reason."""

from datetime import timezone
from zoneinfo import ZoneInfo

from tests.support.clock_instants import BERLIN, SKIPPED_HOUR
from tests.unit.web.test_cabinet_reminder_time_instants import NOTES, UNKNOWN_ZONE, World, _bearer, _wall


async def test_an_update_without_a_due_does_not_need_a_known_zone():
    w = await World().with_user(UNKNOWN_ZONE)
    note_id = await w.existing_note()
    before = w.stored_dues()

    resp = await w.update(note_id, "")

    assert resp.status_code == 200
    assert w.stored_dues() == before


async def test_an_update_with_only_a_new_text_keeps_the_due_on_an_unknown_zone():
    w = await World().with_user(UNKNOWN_ZONE)
    note_id = await w.existing_note()
    before = w.stored_dues()

    resp = await w.client.put(f"/api/user/reminders/{note_id}", headers=_bearer(), json={"text": "water the roses"})

    assert resp.status_code == 200
    assert [d["text"] for d in w.db.collection(NOTES).docs.values()] == ["water the roses"]
    assert w.stored_dues() == before


async def test_the_unknown_zone_refusal_names_the_zone():
    w = await World().with_user(UNKNOWN_ZONE)

    resp = await w.create(_wall(SKIPPED_HOUR[BERLIN].naive))

    assert resp.status_code == 400
    assert UNKNOWN_ZONE in (await resp.get_json())["error"]


async def test_the_skipped_time_refusal_names_the_zone():
    w = await World().with_user(BERLIN)

    resp = await w.create(_wall(SKIPPED_HOUR[BERLIN].naive))

    assert resp.status_code == 400
    assert BERLIN in (await resp.get_json())["error"]


async def test_the_list_on_an_unknown_zone_shows_utc_times_and_warns(caplog):
    """A read must not fail on a bad stored zone: the list answers 200, its times and its `timezone` in UTC."""
    w = await World().with_user(UNKNOWN_ZONE)
    await w.existing_note()
    [due] = w.stored_dues()

    resp = await w.client.get("/api/user/reminders", headers=_bearer())

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["timezone"] == "UTC"
    assert [r["due"] for r in body["reminders"]] == [due.astimezone(timezone.utc).isoformat()]
    assert any(UNKNOWN_ZONE in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


async def test_the_list_on_a_known_zone_shows_local_times():
    """Control: a known zone keeps its local times."""
    w = await World().with_user(BERLIN)
    await w.existing_note()
    [due] = w.stored_dues()

    body = await (await w.client.get("/api/user/reminders", headers=_bearer())).get_json()

    assert body["timezone"] == BERLIN
    assert [r["due"] for r in body["reminders"]] == [due.astimezone(ZoneInfo(BERLIN)).isoformat()]
