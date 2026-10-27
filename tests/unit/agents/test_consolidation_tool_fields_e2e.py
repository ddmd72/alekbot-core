"""P14 card B: the consolidation pass's write tools, every declared field to the stored fact documents.

`ConsolidationAgent.execute` runs for real over a scripted `LLMPort` (card A's `RoutedLLM`): its `_call_llm`, the tool
loop, the tool executor, the fact management adapter, the write service and the repository on the evaluating Firestore
fake. Only the model is scripted. Stage 1 (marker "Begin deliberate consolidation.") answers its scripted tool turns
from a queue and then the report; the rulebook review (Stage 2b, unconditional) answers the report; the cluster pass is
off (`INLINE_CLUSTER_REVIEW = False`). Every assertion reads a stored document or the tool result the model received.
"""
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.entities import FactDomain
from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE
from src.domain.fact_write_context import as_utc
from src.domain.llm import LLMRequest, LLMResponse
from src.domain.request_context import RequestContext
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.redelivery_world import require
from tests.support.scripted_llm import RoutedLLM, execution_context, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused

STAGE1 = "Begin deliberate consolidation."
RULEBOOK = "STANDING DIRECTIVES REVIEW"
REPORT = LLMResponse(text='{"operations": []}')
BERLIN = "Europe/Berlin"


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


# The batch's messages span 2026-10-20 08:00 .. 11:00 UTC; the Stage 1 default date is the span end.
MESSAGES = [
    {"role": "user", "text": "I started at Acme as a data engineer on 1 October.", "created_at": utc(20, 8).isoformat()},
    {"role": "model", "text": "Congratulations on the new job.", "created_at": utc(20, 9).isoformat()},
    {"role": "user", "text": "And I moved from FitLife to the Iron Temple gym.", "created_at": utc(20, 11).isoformat()},
]
SPAN_END = utc(20, 11)
INSIDE = utc(20, 9, 30)                         # strictly inside the span, not its end
SEEDED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
# A horizon counts from the span end; the write path refuses one that ends within a day of the real now (born due).
HORIZON_DAYS = (datetime.now(timezone.utc) - SPAN_END).days + 60

# ------------------------------------------------------------------------------------------- the scripted calls

PASSPORT = "User's passport expires on 2031-03-04."
CREATE_CURRENT: Dict[str, Any] = {
    "content": PASSPORT,
    "fact_attributes": {
        "domain": "legal", "temporal_class": "stable", "state": "current", "context_priority": "high",
        "tags": ["passport", "documents"], "metadata": {"document": "passport", "expires": "2031-03-04"},
        "context": "travel documents", "reported_date": INSIDE.isoformat(),
        "expiry": {"kind": "date", "date": "2031-03-04"},
    },
}
FITLIFE_OLD = "User trained at the FitLife gym on Main Street."
CREATE_ARCHIVED: Dict[str, Any] = {
    "content": FITLIFE_OLD,
    "fact_attributes": {
        "domain": "health", "temporal_class": "dynamic", "state": "archived", "context_priority": "low",
        "tags": ["gym"], "expiry": {"kind": "horizon", "days": HORIZON_DAYS},
        "derived_from": ["seed-fitlife"],
    },
}
JOB = "User works as a data engineer at Acme since 2026-10-01."
UPDATE_VERSION: Dict[str, Any] = {
    "fact_id": "seed-job",
    "updates": {
        "content": JOB, "tags": ["job"], "metadata": {"since": "2026-10-01"}, "temporal_class": "stable",
        "reported_date": INSIDE.isoformat(), "expiry": {"kind": "horizon", "days": HORIZON_DAYS},
    },
}
UPDATE_SUPERSEDE: Dict[str, Any] = {"fact_id": "seed-fitlife", "updates": {"state": "superseded",
                                                                           "superseded_by": "seed-iron"}}
UPDATE_CLOSE: Dict[str, Any] = {"fact_id": "seed-car",
                                "updates": {"state": "archived", "expiry": {"kind": "date", "date": "2031-03-04"}}}
MERGED_PASSPORT = "User's passport is valid until 2031-03-04."
MERGE_DATE: Dict[str, Any] = {
    "fact_ids": ["seed-passport-a", "seed-passport-b"],
    "merged_content": MERGED_PASSPORT,
    "fact_attributes": {
        "domain": "legal", "temporal_class": "stable", "state": "current", "context_priority": "low",
        "tags": ["passport"], "metadata": {"document": "passport"}, "context": "travel documents",
        "reported_date": INSIDE.isoformat(), "expiry": {"kind": "date", "date": "2031-03-04"},
    },
}
MERGED_GYM = "User trains at the Iron Temple gym, after leaving FitLife."
MERGE_HORIZON: Dict[str, Any] = {
    "fact_ids": ["seed-gym-a", "seed-gym-b"],
    "merged_content": MERGED_GYM,
    "fact_attributes": {
        "domain": "health", "temporal_class": "dynamic", "context_priority": "medium", "tags": ["gym"],
        "expiry": {"kind": "horizon", "days": HORIZON_DAYS},
    },
}
MERGED_FITLIFE = "User trained at the FitLife gym on Main Street."
MERGE_CLOSED: Dict[str, Any] = {
    "fact_ids": ["seed-fitlife-a", "seed-fitlife-b"],
    "merged_content": MERGED_FITLIFE,
    "fact_attributes": {
        "domain": "health", "temporal_class": "dynamic", "state": "archived", "context_priority": "low",
        "tags": ["gym"], "expiry": {"kind": "none"},
    },
}
SEARCH_ALL: Dict[str, Any] = {"keywords": ["gym", "Iron Temple"], "primary_query": "User trains at the Iron Temple gym",
                              "alternative_query": "which gym the user goes to", "limit": 5}
COUNT = {"text": JOB}
UPDATE_UNKNOWN: Dict[str, Any] = {"fact_id": "seed-job",
                                  "updates": {"domain": "preference", "context_priority": "low",
                                              "context": "Q1 2026 project"}}
UPDATE_KNOWN: Dict[str, Any] = {"fact_id": "seed-job", "updates": {"content": JOB,
                                                                   "expiry": {"kind": "horizon",
                                                                              "days": HORIZON_DAYS}}}

# ------------------------------------------------------------------------------------------- the rig


@pytest.fixture
def store() -> FactStore:
    return FactStore(indexes=INDEXES_FILE)


def seed(store: FactStore, doc_id: str, text: str, domain: FactDomain, **over: Any) -> str:
    return put_raw(store, doc_id, text, entity={"domain": domain, "reported_date": SEEDED_AT, **over})


class Pass:
    """The scripted model of one consolidation run: Stage 1 drains `stage1`, then reports; the rulebook reports."""

    def __init__(self, *stage1: LLMResponse) -> None:
        self.stage1: List[LLMResponse] = list(stage1)
        self.llm = RoutedLLM(self._answer, pending=lambda: len(self.stage1))

    def _answer(self, request: LLMRequest) -> Any:
        first = request.messages[0].parts[0].text or ""
        if STAGE1 in first:
            return self.stage1.pop(0) if self.stage1 else REPORT
        if RULEBOOK in first:
            return REPORT
        return None  # an unexpected pass: the fake raises ScriptExhausted


async def run(store: FactStore, script: Pass):
    port = FactManagementAdapter(store.repo, store.embedder, store.write,
                                 SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context(script.llm, "consolidation"),
        repository=store.repo, embedding_service=store.embedder, fact_write_service=store.write,
        fact_management_port=port, fact_updates=port, prompt_builder=StubPromptBuilder(),
    )
    agent.INLINE_CLUSTER_REVIEW = False
    message = AgentMessage(task_id="t", sender="consolidation_queue", recipient="consolidation_agent",
                           intent=AgentIntent.DELEGATE, payload={"task": "consolidate", "messages": MESSAGES},
                           context={"user_id": USER, "owner_timezone": BERLIN})
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent.execute(message)


def by_text(store: FactStore, text: str) -> Dict[str, Any]:
    (doc,) = [d for d in store.docs.values() if d["text"] == text]
    return doc


def results(llm: RoutedLLM, name: str) -> List[Any]:
    return [r for n, r in tool_results(llm) if n == name]


def script_spent(llm: RoutedLLM) -> None:
    assert llm.unanswered == 0 and llm.pending() == 0


# ------------------------------------------------------------------------------------------- the tests


async def test_create_fact_writes_every_attribute(store):
    seed(store, "seed-fitlife", "User trains at the FitLife gym.", FactDomain.HEALTH)
    script = Pass(tools(("create_fact", CREATE_CURRENT), ("create_fact", CREATE_ARCHIVED)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    current, archived = by_text(store, PASSPORT), by_text(store, FITLIFE_OLD)
    assert (current["domain"], current["temporal_class"], current["state"], current["context_priority"]) == (
        "legal", "stable", "current", "high")
    assert current["tags"] == ["passport", "documents", "consolidated"]
    assert current["metadata"] == {"document": "passport", "expires": "2031-03-04"}
    assert current["context"] == "travel documents"
    assert as_utc(current["reported_date"]) == INSIDE
    assert current["expiry_kind"] == "date"
    assert as_utc(current["expires_at"]) == datetime(2031, 3, 4, 23, tzinfo=timezone.utc)  # end of the day, Berlin

    assert (archived["domain"], archived["temporal_class"], archived["state"], archived["context_priority"]) == (
        "health", "dynamic", "archived", "low")
    assert archived["tags"] == ["gym", "consolidated"]
    assert archived.get("expires_at") is None and archived.get("expiry_kind") is None
    first, second = results(script.llm, "create_fact")
    assert "note" not in first or "derived_from" not in first["note"]
    assert "derived_from" in second["note"] and EXPIRY_IGNORED_ON_CLOSE in second["note"]
    script_spent(script.llm)


async def test_update_fact_content_change_writes_a_version_with_every_field(store):
    seed(store, "seed-job", "User works at Acme.", FactDomain.WORK, tags=["work", "anchor"],
         metadata={"employer": "Acme"})
    script = Pass(tools(("update_fact", UPDATE_VERSION)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    (result,) = results(script.llm, "update_fact")
    new = store.docs[result["fact_id"]]
    assert result["fact_id"] != "seed-job"
    assert new["text"] == JOB
    assert new["tags"] == ["job", "anchor"]                                # the given tags, then the system tags kept
    assert new["metadata"] == {"employer": "Acme", "since": "2026-10-01"}  # merged key by key (§1.3.5)
    assert new["temporal_class"] == "stable"
    assert as_utc(new["reported_date"]) == INSIDE
    assert new["expiry_kind"] == "horizon"
    assert as_utc(new["expires_at"]) == INSIDE + timedelta(days=HORIZON_DAYS)
    assert store.docs["seed-job"]["superseded_by"] == result["fact_id"]
    script_spent(script.llm)


async def test_update_fact_supersede_and_close(store):
    seed(store, "seed-fitlife", "User trains at the FitLife gym.", FactDomain.HEALTH)
    seed(store, "seed-iron", "User trains at the Iron Temple gym.", FactDomain.HEALTH)
    seed(store, "seed-car", "User's car service is on 2026-11-05.", FactDomain.POSSESSION)
    script = Pass(tools(("update_fact", UPDATE_SUPERSEDE), ("update_fact", UPDATE_CLOSE)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    assert store.docs["seed-fitlife"]["state"] == "superseded"
    assert store.docs["seed-fitlife"]["superseded_by"] == "seed-iron"
    assert store.docs["seed-car"]["state"] == "archived"
    supersede, close = results(script.llm, "update_fact")
    assert supersede["status"] == "updated" and close["status"] == "updated"
    assert EXPIRY_IGNORED_ON_CLOSE in close["note"]
    script_spent(script.llm)


async def test_merge_facts_writes_every_attribute(store):
    seed(store, "seed-passport-a", "User has a passport.", FactDomain.LEGAL)
    seed(store, "seed-passport-b", "User's passport expires on 2031-03-04.", FactDomain.LEGAL)
    seed(store, "seed-gym-a", "User trains at the Iron Temple gym.", FactDomain.HEALTH)
    seed(store, "seed-gym-b", "User left the FitLife gym.", FactDomain.HEALTH)
    script = Pass(tools(("merge_facts", MERGE_DATE), ("merge_facts", MERGE_HORIZON)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    passport, gym = by_text(store, MERGED_PASSPORT), by_text(store, MERGED_GYM)
    assert (passport["domain"], passport["temporal_class"], passport["state"], passport["context_priority"]) == (
        "legal", "stable", "current", "low")
    assert passport["tags"] == ["passport", "consolidated"]
    assert passport["metadata"] == {"document": "passport"}
    assert passport["context"] == "travel documents"
    assert as_utc(passport["reported_date"]) == INSIDE
    assert passport["expiry_kind"] == "date"
    assert as_utc(passport["expires_at"]) == datetime(2031, 3, 4, 23, tzinfo=timezone.utc)
    assert (gym["domain"], gym["temporal_class"], gym["context_priority"], gym["expiry_kind"]) == (
        "health", "dynamic", "medium", "horizon")
    assert as_utc(gym["expires_at"]) == SPAN_END + timedelta(days=HORIZON_DAYS)
    passport_id = next(k for k, d in store.docs.items() if d["text"] == MERGED_PASSPORT)
    gym_id = next(k for k, d in store.docs.items() if d["text"] == MERGED_GYM)
    assert [store.docs[s]["superseded_by"] for s in ("seed-passport-a", "seed-passport-b")] == [passport_id] * 2
    assert [store.docs[s]["superseded_by"] for s in ("seed-gym-a", "seed-gym-b")] == [gym_id] * 2
    script_spent(script.llm)


async def test_merge_facts_into_a_closed_fact(store):
    """`fact_attributes.state` of a merge is stored: the merged fact is written archived, and both sources point to it."""
    seed(store, "seed-fitlife-a", "User trains at the FitLife gym.", FactDomain.HEALTH)
    seed(store, "seed-fitlife-b", "User's gym is FitLife on Main Street.", FactDomain.HEALTH)
    script = Pass(tools(("merge_facts", MERGE_CLOSED)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    merged_id = next(k for k, d in store.docs.items() if d["text"] == MERGED_FITLIFE)
    assert store.docs[merged_id]["state"] == "archived"
    assert [store.docs[s]["superseded_by"] for s in ("seed-fitlife-a", "seed-fitlife-b")] == [merged_id] * 2
    script_spent(script.llm)


async def test_search_and_count_write_nothing(store):
    seed(store, "seed-iron", "User trains at the Iron Temple gym.", FactDomain.HEALTH)
    before = copy.deepcopy(store.docs)
    script = Pass(tools(("search_existing_facts", SEARCH_ALL), ("count_words", COUNT)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    assert store.docs == before
    (search,) = results(script.llm, "search_existing_facts")
    (count,) = results(script.llm, "count_words")
    assert "error" not in search
    assert count["word_count"] == 10
    script_spent(script.llm)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P14-D1: an update of only unknown keys reports `updated` and names none of them")
async def test_an_update_of_only_unknown_keys_is_not_reported_as_updated(store):
    """`parse_fact_update` (src/domain/fact_update.py) drops keys it does not know (`domain`, `context_priority`,
    `context`) without a word, so the model is told `updated` for a call that changed nothing it asked for.
    Fix: `parse_fact_update` adds a note naming the keys it ignored; parser notes already reach `note` through
    `finish_fact_update` (src/domain/fact_update_lifecycle.py) and `with_notes` (src/domain/fact_tool_result.py)."""
    seed(store, "seed-job", "User works at Acme.", FactDomain.WORK)
    script = Pass(tools(("update_fact", UPDATE_UNKNOWN)))

    await run(store, script)

    require(script.llm.unanswered == 0 and script.llm.pending() == 0, "the script is consumed exactly")
    found = results(script.llm, "update_fact")
    require(len(found) == 1 and isinstance(found[0], dict), "the update_fact result reached the model")
    require(store.docs["seed-job"]["domain"] == "work", "the stored domain is unchanged")
    result = found[0]
    assert result.get("status") != "updated" or "domain" in result.get("note", "")


async def test_an_update_with_a_known_key_is_reported_as_updated(store):
    seed(store, "seed-job", "User works at Acme.", FactDomain.WORK)
    script = Pass(tools(("update_fact", UPDATE_KNOWN)))

    await run(store, script)

    (result,) = results(script.llm, "update_fact")
    assert result["status"] == "updated"
    assert store.docs[result["fact_id"]]["text"] == JOB
    assert store.docs["seed-job"]["superseded_by"] == result["fact_id"]
    script_spent(script.llm)


def test_the_scripted_calls_use_every_declared_field():
    assert unused("consolidation", "create_fact", [CREATE_CURRENT, CREATE_ARCHIVED]) == []
    assert unused("consolidation", "update_fact",
                  [UPDATE_VERSION, UPDATE_SUPERSEDE, UPDATE_CLOSE, UPDATE_KNOWN]) == []
    assert unused("consolidation", "merge_facts", [MERGE_DATE, MERGE_HORIZON, MERGE_CLOSED]) == []
    assert unused("consolidation", "search_existing_facts", [SEARCH_ALL]) == []
    assert unused("consolidation", "count_words", [COUNT]) == []
