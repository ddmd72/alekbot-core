"""Facts S4 · T11a: the Cabinet's fact lists by state, through the real routes, repository and review adapters on the
evaluating Firestore fake: the browse route's `state` filter (stale rows are paginated, a bad state is a 400, the default is
the current facts) and the read-only `recently-closed` list (both closed states, the window, the order, the trail actor,
tenancy). Nothing is written by any of these routes."""
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, List
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.domain.entities import FactState
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, OTHER, USER, make_fact
from tests.support.fact_lifecycle_world import make_fact_world
from src.composition.connector_registry import ConnectorRegistry

TOKEN = {"Authorization": "Bearer t"}


def _app(repo: Any, account: str = ACC) -> Quart:
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": account, "role": "owner"})
    app = Quart("test_cabinet_fact_state_views")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=repo,
        embedding_service=MagicMock(), connectors=ConnectorRegistry(),
    ))
    return app


async def _get(repo: Any, path: str, account: str = ACC):
    async with _app(repo, account).test_client() as client:
        resp = await client.get(path, headers=TOKEN)
        return resp.status_code, await resp.get_json()


@pytest.fixture
def world():
    store, repo, _ = make_fact_world()
    return store, repo


def _closed(text: str, state: FactState, closed_days_ago: float, actor: str, account: str = ACC):
    closed_at = datetime.now(timezone.utc) - timedelta(days=closed_days_ago)
    return make_fact(
        text, account=account, state=state, is_current=False, valid_to=closed_at,
        lifecycle_events=[{"at": closed_at, "from": "stale", "to": state.value, "actor": actor, "reason": "test"}],
    )


# ------------------------------------------------------------------------------------------------ browse by state

async def test_browse_without_a_state_lists_the_current_facts_only(world):
    store, repo = world
    await repo.add_fact(make_fact("likes oolong", age_days=1))
    await repo.add_fact(make_fact("walks to the market", age_days=2, state=FactState.STALE))

    status, body = await _get(repo, "/api/user/facts/browse")

    assert status == 200
    assert [f["text"] for f in body["facts"]] == ["likes oolong"]
    assert body["facts"][0]["state"] == "current"


async def test_browse_of_the_stale_state_lists_the_stale_facts_with_their_age(world):
    store, repo = world
    stale_since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await repo.add_fact(make_fact("likes oolong", age_days=1))
    await repo.add_fact(make_fact("walks to the market", age_days=2, state=FactState.STALE, stale_since=stale_since))

    status, body = await _get(repo, "/api/user/facts/browse?state=stale")

    assert status == 200
    [row] = body["facts"]
    assert (row["text"], row["state"], row["stale_since"]) == ("walks to the market", "stale", stale_since.isoformat())


async def test_stale_rows_are_paginated_without_overlap_or_gap(world):
    store, repo = world
    for age in range(5):
        await repo.add_fact(make_fact(f"stale fact {age}", age_days=age, state=FactState.STALE))
    await repo.add_fact(make_fact("current fact", age_days=9))

    seen: List[str] = []
    cursor = None
    pages = 0
    while True:
        path = "/api/user/facts/browse?state=stale&limit=2" + (f"&cursor={cursor}" if cursor else "")
        status, body = await _get(repo, path)
        assert status == 200 and len(body["facts"]) <= 2
        seen += [f["text"] for f in body["facts"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3 and sorted(seen) == [f"stale fact {age}" for age in range(5)] and len(set(seen)) == 5


@pytest.mark.parametrize("state", ["archived", "invalidated", "superseded", "STALE", "all", "x"])
async def test_a_state_that_is_not_listable_is_a_400_and_nothing_is_read(world, state):
    store, repo = world
    await repo.add_fact(make_fact("likes oolong"))
    before = copy.deepcopy(store.docs)

    status, body = await _get(repo, f"/api/user/facts/browse?state={state}")

    assert status == 400 and "state" in body["error"]
    assert store.docs == before


async def test_browse_by_state_lists_only_the_sessions_account(world):
    store, repo = world
    await repo.add_fact(make_fact("mine", state=FactState.STALE))
    await repo.add_fact(make_fact("theirs", account=OTHER, state=FactState.STALE))

    _, body = await _get(repo, "/api/user/facts/browse?state=stale")

    assert [f["text"] for f in body["facts"]] == ["mine"]


async def test_the_domain_filter_still_narrows_a_stale_list(world):
    from src.domain.entities import FactDomain
    store, repo = world
    await repo.add_fact(make_fact("a", state=FactState.STALE, domain=FactDomain.HEALTH))
    await repo.add_fact(make_fact("b", state=FactState.STALE, domain=FactDomain.FINANCE))

    _, body = await _get(repo, "/api/user/facts/browse?state=stale&domain=finance")

    assert [f["text"] for f in body["facts"]] == ["b"]


# ------------------------------------------------------------------------------------------------ recently closed

async def test_recently_closed_lists_both_closed_states_newest_closure_first_with_the_trail_actor(world):
    store, repo = world
    await repo.add_fact(_closed("drank green tea", FactState.ARCHIVED, closed_days_ago=3, actor="review"))
    await repo.add_fact(_closed("wore a red scarf", FactState.INVALIDATED, closed_days_ago=1, actor="owner"))
    await repo.add_fact(_closed("lived in Lisbon", FactState.ARCHIVED, closed_days_ago=5, actor="timeout"))

    status, body = await _get(repo, "/api/user/facts/recently-closed")

    assert status == 200
    assert [(f["text"], f["state"], f["actor"]) for f in body["facts"]] == [
        ("wore a red scarf", "invalidated", "owner"),
        ("drank green tea", "archived", "review"),
        ("lived in Lisbon", "archived", "timeout"),
    ]
    assert all(f["closed_at"] for f in body["facts"])


async def test_recently_closed_leaves_out_what_closed_before_the_window(world):
    store, repo = world
    await repo.add_fact(_closed("inside", FactState.ARCHIVED, closed_days_ago=29, actor="review"))
    await repo.add_fact(_closed("outside", FactState.ARCHIVED, closed_days_ago=31, actor="review"))

    _, body = await _get(repo, "/api/user/facts/recently-closed")

    assert [f["text"] for f in body["facts"]] == ["inside"]


async def test_recently_closed_lists_everything_closed_whoever_closed_it_each_with_its_actor(world):
    store, repo = world
    await repo.add_fact(_closed("by the review", FactState.ARCHIVED, 1, "review"))
    await repo.add_fact(_closed("by the cabinet", FactState.INVALIDATED, 2, "cabinet"))
    await repo.add_fact(_closed("by the consolidator", FactState.INVALIDATED, 3, "stage1"))
    await repo.add_fact(make_fact("still current"))
    await repo.add_fact(make_fact("waiting", state=FactState.STALE))

    _, body = await _get(repo, "/api/user/facts/recently-closed")

    assert [(f["text"], f["actor"]) for f in body["facts"]] == [
        ("by the review", "review"), ("by the cabinet", "cabinet"), ("by the consolidator", "stage1"),
    ]


async def test_recently_closed_lists_at_most_fifty_newest_first(world):
    store, repo = world
    for n in range(52):
        await repo.add_fact(_closed(f"closed {n}", FactState.ARCHIVED, 0.01 * (n + 1), "review"))

    _, body = await _get(repo, "/api/user/facts/recently-closed")

    assert [f["text"] for f in body["facts"]] == [f"closed {n}" for n in range(50)]


async def test_recently_closed_lists_only_the_sessions_account(world):
    store, repo = world
    await repo.add_fact(_closed("mine", FactState.ARCHIVED, 1, "review"))
    await repo.add_fact(_closed("theirs", FactState.ARCHIVED, 1, "review", account=OTHER))

    _, mine = await _get(repo, "/api/user/facts/recently-closed")
    _, theirs = await _get(repo, "/api/user/facts/recently-closed", account=OTHER)

    assert [f["text"] for f in mine["facts"]] == ["mine"] and [f["text"] for f in theirs["facts"]] == ["theirs"]


async def test_recently_closed_writes_nothing(world):
    store, repo = world
    await repo.add_fact(_closed("a", FactState.ARCHIVED, 1, "review"))
    before = copy.deepcopy(store.docs)

    await _get(repo, "/api/user/facts/recently-closed")

    assert store.docs == before


async def test_recently_closed_is_a_500_without_details_when_the_store_fails():
    repo = MagicMock()
    repo.get_recently_closed = MagicMock(side_effect=RuntimeError("store down: secret detail"))

    status, body = await _get(repo, "/api/user/facts/recently-closed")

    assert status == 500 and body == {"error": "Internal server error"}
