"""Facts S3 · T12b review M1: on the kit, search and Stage 2a show what production would show after the run's own writes.

Vector search sees the live store only, so without a correction a fact the run closed still comes back as current and a
fact the run wrote is missing from Stage 2a's cluster.
"""
import pytest

from src.domain.agent import AgentIntent, AgentMessage
from src.domain.llm import ToolCall
from src.domain.request_context import RequestContext
from tests.support.fact_kit import ACC, USER
from tests.support.fact_write import EXPIRY_NONE, horizon
from tests.unit.agents.test_consolidation_write_context import calls, create
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (fixtures: store, memory_dir)
    answered,
    kit_agent,
    memory_dir,
    metered,
    seed,
    store,
)


async def search(agent, words: str):
    async with RequestContext(user_id=USER, account_id=ACC):
        return await agent._fact_management.search_existing_facts(
            words.split(), words, limit=20, timezone="UTC")


async def test_a_search_hit_the_run_replaced_shows_its_successor_and_one_the_run_closed_is_dropped(store, tmp_path):
    kept = await seed(store, "cooks on sundays")
    rewritten = await seed(store, "walks daily")
    invalidated = await seed(store, "plays chess on fridays")
    agent, kit = await kit_agent(store, tmp_path, answered())
    context = kit.write_context("stage1")
    version = await agent._fact_management.update_fact(
        rewritten, {"content": "walks daily in the park", "expiry": horizon(40)}, ACC, context=context)
    await agent._fact_management.update_fact(invalidated, {"state": "invalidated"}, ACC, context=context)

    hits = await search(agent, "walks daily chess fridays cooks sundays")

    by_id = {h["fact_id"]: h for h in hits}
    assert kept in by_id and rewritten not in by_id and invalidated not in by_id
    assert by_id[version["fact_id"]]["content"] == "walks daily in the park"            # the successor, with its own decision
    assert by_id[version["fact_id"]]["expiry"] == horizon(40) and by_id[version["fact_id"]]["state"] == "current"
    assert by_id[version["fact_id"]]["version"] == 2
    assert len(by_id) == len(hits)                                                          # no id twice


async def test_a_hit_the_run_changed_in_place_shows_the_overlay_document(store, tmp_path):
    fact = await seed(store, "walks daily")                                             # seeded with no expiry
    agent, kit = await kit_agent(store, tmp_path, answered())
    changed = await agent._fact_management.update_fact(fact, {"expiry": horizon(20)}, ACC, context=kit.write_context("stage1"))

    hits = {h["fact_id"]: h for h in await search(agent, "walks daily")}
    live = await store.repo.get_fact_by_id(fact)

    assert changed["status"] == "updated" and changed["fact_id"] == fact               # in place: same document
    assert live.expiry_kind.value == "none"                                            # the live store is untouched
    assert hits[fact]["expiry"] == horizon(20) and hits[fact]["due"]                  # the hit shows the run's decision


async def stage2a_cluster_text(agent) -> str:
    return next(first for first in agent.requests if "FACT CLUSTER REVIEW" in first)


async def test_stage_2a_reviews_the_fact_stage_1_wrote_and_not_the_one_it_retired(store, tmp_path):
    compound = await seed(store, "walks daily and reads at night")
    await seed(store, "cooks on sundays")
    stage1 = metered(calls(
        ToolCall(name="update_fact", args={"fact_id": compound, "updates": {"content": "walks daily", "expiry": EXPIRY_NONE}}),
        create("flight booked", expiry=horizon(30)),
    ))
    agent, kit = await kit_agent(store, tmp_path, stage1, answered(), answered())
    agent.INLINE_CLUSTER_REVIEW = True
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": [{"role": "user", "text": "booked a flight", "created_at": _now()}]},
        context={"user_id": USER, "owner_timezone": "UTC"},
    )

    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)

    assert response.status.value == "success", response.error
    assert [p["pass"] for p in kit.passes] == ["stage1", "stage2a"]
    rewritten, flight = [r["fact_id"] for r in kit.rows]
    text = await stage2a_cluster_text(agent)
    assert compound not in text                                                   # the retired compound is not offered as current
    assert text.index(rewritten) < text.index(flight)                            # the run's own facts lead the cluster, in order
    assert '"walks daily"' in text and "flight booked" in text
    assert "cooks on sundays" in text                                              # and the live neighbours follow


async def test_stage_2a_is_still_built_when_the_searches_find_nothing_but_the_run_wrote_a_fact(store, tmp_path):
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create("flight booked", expiry=horizon(30)))), answered(), answered())
    agent.INLINE_CLUSTER_REVIEW = True
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": [{"role": "user", "text": "booked a flight", "created_at": _now()}]},
        context={"user_id": USER, "owner_timezone": "UTC"},
    )

    with RequestContext(user_id=USER, account_id=ACC):
        await agent.execute(message)

    assert [p["pass"] for p in kit.passes] == ["stage1", "stage2a"]


def _now() -> float:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).timestamp()
