"""Facts S3 · T12b review M1: on the kit, search and Stage 2a show what production would show after the run's own writes.

Vector search sees the live store only, so without a correction a fact the run closed still comes back as current and a
fact the run wrote is missing from Stage 2a's cluster.
"""
import json
import re

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
    assert rewritten in text and flight in text                                  # the run's own facts are in the cluster
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


def cluster_entries(text: str):
    return [json.loads(m.group(1)) for line in text.splitlines() if (m := re.match(r"^\d+\. (\{.*\})$", line))]


def words(entry) -> int:
    return len(entry["content"].split())


TOPICS = ("walks daily", "reads at night", "plays chess", "keeps bees")


async def stage1_with_two_short_writes(store, tmp_path, neighbours: int):
    """Live: `neighbours` facts of distinct lengths (100 + n words) on four topics. Stage 1 writes one short fact per
    topic, then Stage 2a runs."""
    for number in range(neighbours):
        topic = TOPICS[number % len(TOPICS)]
        await seed(store, " ".join([topic] + [f"word{number}x{i}" for i in range(100 + number - len(topic.split()))]))
    agent, kit = await kit_agent(
        store, tmp_path, metered(calls(*[create(topic, expiry=EXPIRY_NONE) for topic in TOPICS])), answered(), answered())
    agent.INLINE_CLUSTER_REVIEW = True
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": [{"role": "user", "text": "walks daily", "created_at": _now()}]},
        context={"user_id": USER, "owner_timezone": "UTC"},
    )
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    assert response.status.value == "success", response.error
    own_ids = {r["fact_id"] for r in kit.rows}
    return agent, kit, cluster_entries(await stage2a_cluster_text(agent)), own_ids


async def test_stage_2a_keeps_production_rule_the_thirty_longest_so_short_writes_are_cut_by_long_neighbours(store, tmp_path):
    agent, kit, cluster, own_ids = await stage1_with_two_short_writes(store, tmp_path, neighbours=120)

    found = {}
    for content in TOPICS:                                  # what the searches return, on the kit
        async with RequestContext(user_id=USER, account_id=ACC):                       # as the cluster builder calls it
            hits = await agent._fact_management.search_existing_facts(
                [w for w in content.split()[:10] if len(w) > 3], content, "", 20, timezone="UTC")
        for hit in hits:
            found[hit["fact_id"]] = hit
    expected = sorted(found.values(), key=words, reverse=True)[:30]
    assert len(found) > 30                                                             # enough long hits to fill the cluster

    assert len(cluster) == 30 and not own_ids & {e["fact_id"] for e in cluster}         # the short writes lose to 30 long neighbours
    assert {e["fact_id"] for e in cluster} == {e["fact_id"] for e in expected}
    assert [words(e) for e in cluster] == sorted((words(e) for e in cluster), reverse=True)


async def test_stage_2a_holds_the_short_writes_among_a_few_neighbours_in_length_order(store, tmp_path):
    agent, kit, cluster, own_ids = await stage1_with_two_short_writes(store, tmp_path, neighbours=3)

    assert own_ids <= {e["fact_id"] for e in cluster} and len(cluster) == 7
    assert [words(e) for e in cluster] == sorted((words(e) for e in cluster), reverse=True)
    assert {e["fact_id"] for e in cluster[-4:]} == own_ids                                  # the short ones come last


async def test_a_corrected_search_still_returns_its_limit_when_the_run_closed_some_of_the_best_hits(store, tmp_path):
    for number in range(8):
        await seed(store, f"walks daily with friend number {number}")
    agent, kit = await kit_agent(store, tmp_path, answered())
    best = [h["fact_id"] for h in await search_limit(agent, "walks daily with friend", 4)][:2]
    for fact_id in best:
        await agent._fact_management.update_fact(fact_id, {"state": "invalidated"}, ACC, context=kit.write_context("stage1"))

    hits = await search_limit(agent, "walks daily with friend", 4)

    assert len(hits) == 4 and not set(best) & {h["fact_id"] for h in hits}


async def search_limit(agent, words_: str, limit: int):
    async with RequestContext(user_id=USER, account_id=ACC):
        return await agent._fact_management.search_existing_facts(words_.split(), words_, limit=limit, timezone="UTC")
