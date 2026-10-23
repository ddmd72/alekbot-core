"""Facts S2 · T5: the consolidator's fact-management port is a parameter, one tracker serves a whole run.

The fake port (`ScriptedPort`) keeps the semantics that matter here: it answers with the S2 result shapes (`fact_id` of the
document written, `status`), and it records which pass made each call. The LLM is a script keyed by the system prompt, so
two runs gathered on one agent can be told apart. Every concurrency test asserts the interleaving happened.
"""
import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.consolidation_agent import ConsolidationAgent, _TrackingFactManagement
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.fact_management_port import FactManagementPort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_store import INDEXES_FILE, FactStore

REPORT = LLMResponse(text='{"operations": []}')


class ScriptedPort(FactManagementPort):
    """Answers with the S2 result shapes; `calls` is the (method, text) log in arrival order."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self._n = 0

    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20):
        self.calls.append(("search", primary_query))
        return []

    async def create_fact(self, content, metadata):
        self._n += 1
        self.calls.append(("create", content))
        return {"fact_id": f"new-{content}", "status": "created", "message": "ok"}

    async def update_fact(self, fact_id, updates, account_id):
        self.calls.append(("update", fact_id))
        if updates.get("content"):
            return {"fact_id": f"v2-{fact_id}", "status": "updated", "version": 2, "message": "ok"}
        return {"fact_id": fact_id, "status": "updated", "message": "ok"}

    async def merge_facts(self, fact_ids, merged_content, metadata):
        self.calls.append(("merge", merged_content))
        return {"new_fact_id": f"m-{merged_content}", "old_fact_ids": fact_ids, "status": "merged", "message": "ok"}


def make_agent(port=None, *, repo=None):
    execution_context = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )
    if repo is None:
        repo = MagicMock()
        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        repo.refresh_biographical_context_cache = AsyncMock()
        repo.get_active_facts_ordered = AsyncMock(return_value=[])
    pb = MagicMock()
    pb.build_for_agent = AsyncMock(side_effect=lambda **kw: "SYS|" + "|".join(
        str(m["content"]) for m in kw["conversation_history"]))
    pb.invalidate_biographical_cache = MagicMock()
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context, repository=repo, embedding_service=MagicMock(),
        fact_write_service=MagicMock(), fact_management_port=port, prompt_builder=pb,
    )
    agent.INLINE_CLUSTER_REVIEW = True
    return agent, repo, pb


def tool(name, **args):
    return ToolCall(name=name, args=args)


def create_args(text):
    return {"content": text, "fact_attributes": {"domain": "biographical", "temporal_class": "stable",
                                                 "context_priority": "medium", "tags": []}}


def turn(*calls):
    return LLMResponse(text=None, tool_calls=list(calls))


def script_llm(agent, scripts: Dict[str, List[LLMResponse]], events=None, yield_between_turns=False):
    """`_call_llm` answers from the script of the request's system prompt; the last response repeats as the report."""
    positions = {key: 0 for key in scripts}

    async def call(request, turn=1):
        key = next(k for k in scripts if request.system_instruction.startswith(k))
        i = positions[key]
        positions[key] += 1
        if events is not None:
            events.append((key, i))
        if yield_between_turns:
            await asyncio.sleep(0)
        return scripts[key][i] if i < len(scripts[key]) else REPORT

    agent._call_llm = call
    return positions


async def run_v3(agent, account, text="hello", user=USER):
    with RequestContext(user_id=user, account_id=account):
        return await agent._execute_deliberate_process_v3(
            [{"role": "user", "text": text}], user, account, [])


# ---------------------------------------------------------------------------------------------- the port is a parameter

async def test_stage1_runs_with_its_tracker_and_never_reassigns_the_agent_port():
    port = ScriptedPort()
    agent, repo, _ = make_agent(port)
    seen: List[Any] = []
    during: List[Any] = []

    async def spy_loop(**kw):
        seen.append((kw["pass_label"], kw["fact_management"]))
        during.append(agent._fact_management)
        if kw["pass_label"] == "stage1":
            await kw["fact_management"].create_fact("first", {})
        return []

    agent._run_consolidation_loop = spy_loop
    agent._build_review_cluster = AsyncMock(return_value=[{"fact_id": "x", "content": "c", "similarity": 0.1}])
    directive = MagicMock(id="d1", text="rule")
    repo.get_active_facts_ordered = AsyncMock(return_value=[directive])

    await run_v3(agent, ACC)

    assert [label for label, _ in seen] == ["stage1", "stage2a", "stage2b"]
    trackers = [fm for _, fm in seen]
    assert isinstance(trackers[0], _TrackingFactManagement) and trackers[0]._real is port
    assert trackers[1] is trackers[0] and trackers[2] is trackers[0]
    assert all(p is port for p in during) and agent._fact_management is port


async def test_the_port_is_still_the_agents_own_after_a_run_that_raised():
    port = ScriptedPort()
    agent, _, _ = make_agent(port)
    agent._run_consolidation_loop = AsyncMock(side_effect=RuntimeError("llm down"))
    with pytest.raises(RuntimeError):
        await run_v3(agent, ACC)
    assert agent._fact_management is port


async def test_the_backstop_and_the_tools_use_the_given_port_and_default_to_the_agents_own():
    own, given = ScriptedPort(), ScriptedPort()
    agent, repo, _ = make_agent(own)
    agent.DIRECTIVE_HARD_CAP = 1
    repo.get_active_facts_ordered = AsyncMock(return_value=[MagicMock(id="a"), MagicMock(id="b")])

    await agent._enforce_directive_cap(ACC, fact_management=given)
    assert given.calls == [("update", "b")] and own.calls == []
    await agent._enforce_directive_cap(ACC)
    assert own.calls == [("update", "b")]

    await agent._execute_fact_management_tools([tool("create_fact", **create_args("t"))], USER, ACC, fact_management=given)
    assert ("create", "t") in given.calls and ("create", "t") not in own.calls


async def test_two_concurrent_passes_on_one_agent_record_only_their_own_writes():
    port = ScriptedPort()
    agent, _, _ = make_agent(port)
    events: List[tuple] = []
    seeds: Dict[str, List[tuple]] = {}

    async def spy_cluster(changed, account_id):
        seeds[account_id] = list(changed)
        return []

    agent._build_review_cluster = spy_cluster
    script_llm(agent, {
        "SYS|pass-A": [turn(tool("create_fact", **create_args("A1"))), turn(tool("create_fact", **create_args("A2"))), REPORT],
        "SYS|pass-B": [turn(tool("create_fact", **create_args("B1"))), turn(tool("create_fact", **create_args("B2"))), REPORT],
    }, events=events, yield_between_turns=True)

    await asyncio.gather(run_v3(agent, "acc-A", "pass-A"), run_v3(agent, "acc-B", "pass-B"))

    first_b = events.index(("SYS|pass-B", 0))
    last_a = events.index(("SYS|pass-A", 2))
    first_a = events.index(("SYS|pass-A", 0))
    last_b = events.index(("SYS|pass-B", 2))
    assert first_b < last_a and first_a < last_b                       # the passes interleaved
    assert seeds["acc-A"] == [("new-A1", "A1"), ("new-A2", "A2")]
    assert seeds["acc-B"] == [("new-B1", "B1"), ("new-B2", "B2")]
    assert agent._fact_management is port


# ---------------------------------------------------------------------------------------------- what the tracker records

@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {"account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
            "context_priority": "medium", "tags": ["family"], "metadata": {}}
    base.update(over)
    return base


async def test_tracker_records_the_result_id_of_a_version(store):
    tracker = _TrackingFactManagement(store.adapter)
    created = await tracker.create_fact("mother is Valentina", meta())
    old = created["fact_id"]
    assert tracker.changed == [(old, "mother is Valentina")]

    result = await tracker.update_fact(old, {"content": "mother is Valentina Ivanova"}, ACC)

    assert result["status"] == "updated" and result["fact_id"] != old
    assert tracker.changed[-1] == (result["fact_id"], "mother is Valentina Ivanova")
    again = await tracker.update_fact(old, {"content": "mother is Valentina I."}, ACC)       # the stale id, followed forward
    assert again["fact_id"] not in (old, result["fact_id"])
    assert tracker.changed[-1] == (again["fact_id"], "mother is Valentina I.") and len(tracker.changed) == 3


async def test_tracker_records_a_merge_by_its_new_id_and_a_state_only_update_as_a_write_without_a_seed(store):
    tracker = _TrackingFactManagement(store.adapter)
    a = (await tracker.create_fact("a", meta()))["fact_id"]
    b = (await tracker.create_fact("b", meta()))["fact_id"]
    merged = await tracker.merge_facts([a, b], "a and b", meta())
    assert merged["status"] == "merged" and tracker.changed[-1] == (merged["new_fact_id"], "a and b")

    quiet = _TrackingFactManagement(store.adapter)
    c = (await store.adapter.create_fact("c", meta()))["fact_id"]
    result = await quiet.update_fact(c, {"state": "archived"}, ACC)
    assert result["status"] == "updated"
    assert quiet.changed == [] and quiet.wrote is True


async def test_tracker_records_nothing_for_not_found_failed_or_refused(store):
    fid = (await store.adapter.create_fact("mother is Valentina", meta()))["fact_id"]
    foreign = (await store.adapter.create_fact("foreign", meta(account_id=OTHER)))["fact_id"]
    tracker = _TrackingFactManagement(store.adapter)

    results = [
        await tracker.update_fact("no-such-id", {"content": "x"}, ACC),                     # not_found
        await tracker.update_fact(foreign, {"content": "x"}, ACC),                          # another account: not_found
        await tracker.update_fact(fid, {"content": "x", "state": "bogus"}, ACC),            # failed: bad enum
        await tracker.update_fact(fid, {"state": "superseded"}, ACC),                       # refused: no successor
        await tracker.update_fact(fid, {"content": "   "}, ACC),                            # refused: empty content
        await tracker.create_fact("x", meta(domain="nonsense")),                            # failed
        await tracker.merge_facts(["no-such-id"], "m", meta()),                             # failed: no source
    ]

    assert [r["status"] for r in results] == ["not_found", "not_found", "failed", "failed", "failed", "failed", "failed"]
    assert tracker.changed == [] and tracker.wrote is False


# ---------------------------------------------------------------------------------------------- the cache after a failed run

def writes_then(exc, label="stage1", write=True):
    """A loop that writes through the run's tracker (when `write`), then raises `exc` in pass `label`."""
    async def loop(**kw):
        if kw["pass_label"] == label:
            if write:
                await kw["fact_management"].create_fact("kept", {})
            raise exc
        return []
    return loop


@pytest.mark.parametrize("label", ["stage1", "stage2a", "stage2b"])
async def test_failed_run_that_wrote_refreshes_the_cache_and_a_clean_failure_does_not(label):
    agent, repo, pb = make_agent(ScriptedPort())
    agent._build_review_cluster = AsyncMock(return_value=[{"fact_id": "x", "content": "c", "similarity": 0.1}])
    repo.get_active_facts_ordered = AsyncMock(return_value=[MagicMock(id="d1", text="rule")])

    async def loop(**kw):
        if kw["pass_label"] == "stage1":
            await kw["fact_management"].create_fact("kept", {})
        if kw["pass_label"] == label:
            raise RuntimeError("provider down")
        return []

    agent._run_consolidation_loop = loop
    with pytest.raises(RuntimeError):
        await run_v3(agent, ACC)
    repo.refresh_biographical_context_cache.assert_awaited_once()
    pb.invalidate_biographical_cache.assert_called_once_with(ACC)

    clean, repo2, pb2 = make_agent(ScriptedPort())
    clean._run_consolidation_loop = writes_then(RuntimeError("provider down"), write=False)
    with pytest.raises(RuntimeError):
        await run_v3(clean, ACC)
    repo2.refresh_biographical_context_cache.assert_not_awaited()
    pb2.invalidate_biographical_cache.assert_not_called()


async def test_a_backstop_invalidation_counts_as_a_write_of_the_run_and_a_failure_mid_backstop_refreshes_the_cache():
    class FailsOnSecondUpdate(ScriptedPort):
        async def update_fact(self, fact_id, updates, account_id):
            if sum(1 for kind, _ in self.calls if kind == "update") >= 1:
                raise RuntimeError("store down")
            return await super().update_fact(fact_id, updates, account_id)

    port = FailsOnSecondUpdate()
    agent, repo, pb = make_agent(port)
    over_cap = [MagicMock(id=f"d{i}", text=f"rule {i}") for i in range(agent.DIRECTIVE_HARD_CAP + 2)]
    repo.get_active_facts_ordered = AsyncMock(return_value=over_cap)
    agent._run_consolidation_loop = AsyncMock(return_value=[])               # Stages 1 and 2b write nothing

    with pytest.raises(RuntimeError):
        await run_v3(agent, ACC)

    assert [kind for kind, _ in port.calls] == ["update"]                     # one invalidation landed, the second raised
    repo.refresh_biographical_context_cache.assert_awaited_once()
    pb.invalidate_biographical_cache.assert_called_once_with(ACC)


async def test_a_failed_write_does_not_count_as_a_write():
    class Refusing(ScriptedPort):
        async def create_fact(self, content, metadata):
            return {"fact_id": None, "status": "failed", "message": "invalid domain"}

    agent, repo, _ = make_agent(Refusing())
    agent._run_consolidation_loop = writes_then(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await run_v3(agent, ACC)
    repo.refresh_biographical_context_cache.assert_not_awaited()


async def test_a_run_that_finished_does_not_refresh_inside_the_process():
    agent, repo, _ = make_agent(ScriptedPort())

    async def loop(**kw):
        await kw["fact_management"].create_fact("kept", {})
        return []

    agent._run_consolidation_loop = loop
    await run_v3(agent, ACC)
    repo.refresh_biographical_context_cache.assert_not_awaited()      # the success path refreshes once, in the handler


async def test_execute_refreshes_once_on_success_and_once_on_a_failure_after_a_write():
    ok, repo_ok, _ = make_agent(ScriptedPort())
    ok.INLINE_CLUSTER_REVIEW = False
    script_llm(ok, {"SYS|": [turn(tool("create_fact", **create_args("A1"))), REPORT]})
    bad, repo_bad, _ = make_agent(ScriptedPort())
    bad._run_consolidation_loop = writes_then(RuntimeError("provider down"))

    msg = AgentMessage.create(sender="t", recipient="consolidation_agent", intent=AgentIntent.DELEGATE,
                              payload={"task": "consolidate", "messages": [{"role": "user", "text": "hi"}]},
                              context={"user_id": USER})
    with RequestContext(user_id=USER, account_id=ACC):
        assert (await ok.execute(msg)).status == AgentStatus.SUCCESS
        assert (await bad.execute(msg)).status == AgentStatus.FAILED
    repo_ok.refresh_biographical_context_cache.assert_awaited_once()
    repo_bad.refresh_biographical_context_cache.assert_awaited_once()


# ---------------------------------------------------------------------------------------------- the email pass

class Email:
    def __init__(self, n):
        self.email_id, self.text, self.category, self.tags = f"e{n}", f"fact {n}", "x", []
        self.email_date = __import__("datetime").datetime(2026, 1, 1)
        self.from_address, self.subject, self.attachments, self.metadata = "a@b", "s", [], {}


class EmailRepo:
    def __init__(self, batches):
        self.batches, self.marked = list(batches), []

    async def get_unconsolidated_batch(self, user_id, limit):
        return self.batches.pop(0) if self.batches else []

    async def mark_consolidated(self, user_id, ids, now):
        self.marked.append(ids)


def email_message():
    return AgentMessage.create(sender="t", recipient="consolidation_agent", intent=AgentIntent.DELEGATE,
                               payload={"task": "consolidate_email", "number_of_batches": 3, "batch_size": 1},
                               context={"user_id": USER})


async def run_email(agent):
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent._handle_consolidate_email(email_message(), USER, ACC, [], 0.0)


def email_agent(repo_batches):
    agent, repo, pb = make_agent(ScriptedPort())
    agent._indexed_email_repo = EmailRepo(repo_batches)
    return agent, repo, pb


async def test_email_pass_failure_after_a_write_refreshes_the_cache():
    agent, repo, pb = email_agent([[Email(1)], [Email(2)]])
    calls = []

    async def loop(**kw):
        calls.append(kw["fact_management"])
        if len(calls) == 1:
            await kw["fact_management"].create_fact("from email 1", {})
            return []
        raise RuntimeError("provider down")

    agent._run_consolidation_loop = loop
    with pytest.raises(RuntimeError):
        await run_email(agent)

    assert len(calls) == 2 and calls[0] is calls[1] and isinstance(calls[0], _TrackingFactManagement)
    assert agent._indexed_email_repo.marked == [["e1"]]               # the failed pass is not marked consolidated
    repo.refresh_biographical_context_cache.assert_awaited_once()
    pb.invalidate_biographical_cache.assert_called_once_with(ACC)


async def test_email_pass_clean_failure_does_not_refresh_and_success_refreshes_once():
    agent, repo, _ = email_agent([[Email(1)]])
    agent._run_consolidation_loop = AsyncMock(side_effect=RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        await run_email(agent)
    repo.refresh_biographical_context_cache.assert_not_awaited()

    ok, repo_ok, _ = email_agent([[Email(1)], [Email(2)], []])

    async def loop(**kw):
        await kw["fact_management"].create_fact("from email", {})
        return [{"action": "CREATE"}]

    ok._run_consolidation_loop = loop
    response = await run_email(ok)
    assert response.status == AgentStatus.SUCCESS and ok._indexed_email_repo.marked == [["e1"], ["e2"]]
    repo_ok.refresh_biographical_context_cache.assert_awaited_once()


# ---------------------------------------------------------------------------------------------- the tool schema

def test_update_fact_schema_names_superseded_by_and_describes_tags():
    agent, _, _ = make_agent(ScriptedPort())
    (decl,) = [d for d in agent._get_tool_declarations() if d["name"] == "update_fact"]
    updates = decl["parameters"]["properties"]["updates"]
    assert "superseded_by" in updates["description"]
    assert updates["properties"]["superseded_by"] == {
        "type": "string",
        "description": "with state SUPERSEDED: id of the fact that replaces this one; required then",
    }
    assert updates["properties"]["tags"]["description"] == "full list; system tags are kept"
