"""The memory inbox's four settings and apps routes sit behind the sign-in only (owner, 2026-11-26): they are connection
settings in Settings -> Connections, not facts, so a set and locked facts PIN does not stop them, while a candidate route
still answers `facts_locked`. Callers and the app are from the invented cast (usr-1 Alex; "Desk assistant")."""

from tests.unit.web.test_cabinet_memory_inbox_settings import LockedPin, World, token


async def test_with_the_pin_locked_the_four_routes_answer_by_their_own_logic():
    world = World(facts_pin=LockedPin())
    await world.auth.repo.save_refresh_token(token(1, "ga"))

    got = await world.call("GET", "/settings")
    assert got.status_code == 200 and (await got.get_json())["triage_interval_days"] == 1
    put = await world.call("PUT", "/settings", {"triage_interval_days": 2})
    assert put.status_code == 200 and world.users.user.config.memory_inbox_triage_interval_days == 2
    assert (await world.call("PUT", "/settings", {"triage_interval_days": 5})).status_code == 400
    apps = await world.call("GET", "/apps")
    assert apps.status_code == 200 and [a["grant_id"] for a in (await apps.get_json())["apps"]] == ["ga"]
    assert (await world.call("DELETE", "/apps/unknown")).status_code == 404
    assert (await world.call("DELETE", "/apps/ga")).status_code == 200
    assert await world.live() == []


async def test_a_candidate_route_still_answers_facts_locked_under_the_same_pin():
    world = World(facts_pin=LockedPin())

    for method, path, body in (("GET", "", None), ("POST", "/cand-1/confirm", {}), ("POST", "/cand-1/reject", None)):
        resp = await world.call(method, path, body)
        assert resp.status_code == 403 and await resp.get_json() == {"error": "facts_locked"}, (method, path)


async def test_the_four_routes_still_refuse_an_anonymous_caller():
    world = World(facts_pin=LockedPin())

    for method, path, body in (("GET", "/settings", None), ("PUT", "/settings", {"triage_interval_days": 2}),
                               ("GET", "/apps", None), ("DELETE", "/apps/ga", None)):
        assert (await world.call(method, path, body, headers=False)).status_code == 401
