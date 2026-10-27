"""The `review_stale_facts` agent task (Facts S4 T6c, plan §3.3, §3.4, §3.17): the prompt, the scope over the run's tracker,
the code-built message, and the result read from the scope and the final report.

The agent runs `execute` with its real loop, executor and close-out over a scripted model; the scope, the typed write path,
the repository and the counters are the production classes on the review world's evaluating fake database."""
import json
from datetime import timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.entities import ExpiryKind
from src.domain.fact_review_language import CANONICAL_LABELS, LABEL_KEYS
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_task import FactReviewTask, ReviewItem, encode_review_task
from src.domain.pass_actor import PassActor
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.infrastructure.agent_config import CONSOLIDATION
from src.infrastructure.agent_manifest import Intent
from src.ports.fact_update_port import FactUpdatePort
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMResponse, ProviderCapabilities, ToolCall
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_p2_labels import RU_P2_LABELS
from tests.support.review_rule_builders import question as build_question

TZ = "Europe/Lisbon"
USER = "abcdef1234567890-tail"
HORIZON = {"kind": "horizon", "days": 30}
REPORT = LLMResponse(text=json.dumps({"operations": []}), tool_calls=[])
RU_LABELS = {
    "memory_review_question": "Это всё ещё так?",
    "memory_option_still_true": "Да",
    "memory_option_ended": "Уже нет",
    "memory_option_never": "Никогда",
    "memory_option_dont_know": "Не помню",
    "memory_reply_hint": "Ответьте на это сообщение цифрой или своими словами.",
    "memory_answer_noted": "Спасибо, записал.",
    "memory_reply_noted": "Понял, учту.",
    "memory_already_answered": "Уже отвечено, спасибо.",
    "memory_fact_changed": "Этот факт тем временем изменился, поэтому я не применил ответ; я к нему ещё вернусь.",
    "memory_question_closed": "Этот вопрос закрыт.",
    "memory_digest_header": "Факты, которые я отложил на этой неделе:",
    "memory_digest_footer": "Чтобы вернуть один, скажите в чате, что это всё ещё так.",
    "memory_digest_archived": "это уже не так",
    "memory_digest_invalidated": "этого никогда не было",
    **RU_P2_LABELS,  # P2 revised (C11): the set is the 28 label keys
}


@pytest.fixture
def world():
    return ReviewWorld()


class Prompts:
    """The prompt builder as the agent uses it: records each build; `fail` raises like a missing profile."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    async def build_for_agent(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise LookupError("no prompt profile consolidation_review")
        return "REVIEW SYSTEM PROMPT"

    def invalidate_biographical_cache(self, account_id: str) -> None:
        pass


class RecordingUpdates(FactUpdatePort):
    """A typed write port installed over the real one, as the dry-run kit installs its recorder: it records each write
    and its context, then writes."""

    def __init__(self, real: FactUpdatePort) -> None:
        self._real = real
        self.calls: List[Any] = []

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None, history=None, expected_id=None):
        self.calls.append((fact_id, context))
        return await self._real.apply_fact_update(
            fact_id, update, account_id, context=context, evidence_at=evidence_at, reason=reason, history=history,
        )


def _tools(*calls):
    return LLMResponse(text=None, tool_calls=[ToolCall(name=n, args=a) for n, a in calls])


def _agent(world, responses, prompts=None):
    it = iter(responses)
    provider = MagicMock(spec=LLMPort)
    provider.generate_content = AsyncMock(side_effect=lambda request: next(it))
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=AgentExecutionContext(
            agent_type="consolidation", provider=provider, model_name="claude-x", tier=PerformanceTier.PERFORMANCE,
            capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
        ),
        repository=world.repo,
        embedding_service=MagicMock(),
        fact_write_service=MagicMock(),
        fact_management_port=world.store.adapter,
        prompt_builder=prompts or Prompts(),
        alert_sink=world.sink,
        lifecycle_counters=world.counters,
        fact_updates=world.store.adapter,
    )
    agent._llm = provider
    agent._now = lambda: world.now
    return agent


async def _stale(world, text):
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60))


async def _evidence(world, text="moved to Lisbon in October"):
    return await world.seed(text, reported_date=NOW - timedelta(days=1))


async def _task(world, *fact_ids, mirror=False, owner_messages=(), questions=(), neighbours=(), versions=()):
    items = [
        ReviewItem(
            fact=await world.repo.get_fact_by_id(fid),
            neighbours=tuple([await world.repo.get_fact_by_id(n) for n in neighbours]),
            versions=tuple(versions),
        )
        for fid in fact_ids
    ]
    return FactReviewTask(items=tuple(items), questions=tuple(questions), mirror=mirror, owner_messages=tuple(owner_messages))


async def _run(agent, task, bio=("bio fact",)):
    message = AgentMessage(
        task_id="t1", sender="fact_review_service", recipient="consolidation_agent", intent=AgentIntent.DELEGATE,
        payload={"task": Intent.REVIEW_STALE_FACTS, **encode_review_task(task),
                 "biographical_context": [{"text": b} for b in bio]},
        context={"user_id": USER, "owner_timezone": TZ},
    )
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent.execute(message)


def _requests(agent):
    return [c.args[0] if c.args else next(iter(c.kwargs.values())) for c in agent._llm.generate_content.await_args_list]


def _user_text(agent):
    return _requests(agent)[0].messages[0].parts[0].text


def _line(text, n=1):
    return json.loads(next(l for l in text.splitlines() if l.startswith(f"{n}. "))[len(f"{n}. "):])


def _on(fact_id):
    return {"kind": "evidence", "evidence_fact_id": fact_id}


# ------------------------------------------------------------------------------------------------ the task

async def test_the_agent_takes_the_review_task(world):
    agent = _agent(world, [])
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": "review_stale_facts"}, context={})
    assert Intent.REVIEW_STALE_FACTS == "review_stale_facts"
    assert await agent.can_handle(message)


@pytest.mark.parametrize("updates, outcome", [
    ({"state": "current", "expiry": HORIZON}, "revived"),
    ({"state": "current", "content": "lives in Lisbon", "expiry": HORIZON}, "rewritten"),
    ({"state": "archived"}, "archived"),
    ({"state": "invalidated"}, "invalidated"),
    ("supersede", "superseded"),
])
async def test_each_outcome_is_returned_from_the_scope(world, updates, outcome):
    fact, ev = await _stale(world, "lives in Porto"), await _evidence(world)
    questions = ()
    if updates == "supersede":
        call = {"fact_id": fact, "updates": {"state": "superseded", "superseded_by": ev}}
    elif outcome == "archived":
        # P2 revised (P2-4): the review archives only on the owner's reply to the fact's question
        questions = (build_question(QuestionStatus.ASKED, fact_id=fact, account_id=ACC, user_id=USER,
                                    reply_text="I left Porto", reply_at=NOW - timedelta(hours=2)),)
        call = {"fact_id": fact, "updates": updates, "basis": {"kind": "reply", "question_id": "q1", "quote": "left Porto"}}
    else:
        call = {"fact_id": fact, "updates": updates, "basis": _on(ev)}
    agent = _agent(world, [_tools(("update_fact", call)), REPORT])

    response = await _run(agent, await _task(world, fact, questions=questions))

    assert response.status == AgentStatus.SUCCESS, response.error
    (decided,) = response.result["outcomes"]
    assert decided["fact_id"] == fact and decided["outcome"] == outcome
    assert decided["basis_kind"] == (None if updates == "supersede" else "reply" if outcome == "archived" else "evidence")
    assert world.docs[decided["written_id"]]["state"] in ("current", outcome)
    assert response.result["asks"] == [] and response.result["kept"] == []


async def test_asks_are_returned_and_nothing_is_written(world):
    fact, kept = await _stale(world, "plays tennis on Sundays"), await _stale(world, "owns a red bike")
    before = {fid: dict(world.docs[fid]) for fid in (fact, kept)}
    agent = _agent(world, [_tools(("ask_owner", {"fact_id": fact, "fallback": "revive"})), REPORT])

    response = await _run(agent, await _task(world, fact, kept))

    assert response.status == AgentStatus.SUCCESS, response.error
    # P2 revised (C8): an ask carries its question kind, texts and folds
    assert response.result["asks"] == [{"fact_id": fact, "fallback": "revive", "fallback_date": None, "noise": False,
                                         "question": "still_true", "texts": {}, "folds": []}]
    assert response.result["outcomes"] == [] and response.result["kept"] == [kept]
    assert {fid: world.docs[fid] for fid in (fact, kept)} == before


async def test_the_prompt_is_the_review_profile_without_directives_or_history(world):
    fact = await _stale(world, "plays tennis on Sundays")
    prompts = Prompts()
    agent = _agent(world, [REPORT], prompts)

    await _run(agent, await _task(world, fact), bio=("lives in Lisbon",))

    (call,) = prompts.calls
    assert call["agent_type"] == "consolidation_review"
    assert call["include_directives"] is False and call["conversation_history"] == []
    assert call["biographical_facts"] == [{"text": "lives in Lisbon"}]
    assert _requests(agent)[0].system_instruction == "REVIEW SYSTEM PROMPT"


async def test_a_prompt_failure_is_a_failure_response_and_no_model_call(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [REPORT], Prompts(fail=True))

    response = await _run(agent, await _task(world, fact))

    assert response.status == AgentStatus.FAILED and "review prompt failed" in response.error
    assert agent._llm.generate_content.await_count == 0


async def test_a_model_failure_in_the_loop_is_a_failure_response_and_the_fact_is_left(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [])
    agent._llm.generate_content = AsyncMock(side_effect=RuntimeError("provider down"))

    response = await _run(agent, await _task(world, fact))

    assert response is not None and response.status == AgentStatus.FAILED
    assert response.error == "review_stale_facts failed: provider down"
    assert world.docs[fact]["state"] == "stale"


async def test_a_payload_without_a_review_task_is_a_failure(world):
    agent = _agent(world, [REPORT])
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": "review_stale_facts", "review": {"items": []}},
                           context={"user_id": USER, "owner_timezone": TZ})
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    assert response.status == AgentStatus.FAILED and "not valid" in response.error
    assert agent._llm.generate_content.await_count == 0


async def test_the_owners_zone_and_the_review_label_reach_the_write_context(world):
    fact, ev = await _stale(world, "lives in Porto"), await _evidence(world)
    # P2 revised (P2-2): evidence backs no archive; the write is an invalidation it backs
    agent = _agent(world, [_tools(("update_fact", {"fact_id": fact, "updates": {"state": "invalidated"}, "basis": _on(ev)})),
                           REPORT])
    recorder = RecordingUpdates(world.store.adapter)
    agent._fact_updates = recorder
    agent._stats_alerts = True

    await _run(agent, await _task(world, fact))

    ((written, context),) = recorder.calls
    assert written == fact and context.timezone == TZ and context.actor is PassActor.REVIEW
    assert f"({TZ}, the owner's timezone)" in _user_text(agent)
    assert any(t.startswith("📊") and "pass `review`" in t for t in world.sink.texts)


async def test_the_turn_cap_is_the_review_cap(world):
    fact = await _stale(world, "plays tennis on Sundays")
    turns = CONSOLIDATION.review_max_turns
    agent = _agent(world, [_tools(("count_words", {"text": "a"})) for _ in range(turns + 1)])

    response = await _run(agent, await _task(world, fact))

    assert agent._llm.generate_content.await_count == turns
    assert response.status == AgentStatus.SUCCESS
    assert response.result["kept"] == [fact] and response.result["message_language"] is None
    assert any("turn limit" in t and f"turns {turns}/{turns}" in t for t in world.sink.texts)


async def test_the_scope_writes_through_a_port_installed_after_construction(world):
    fact, ev = await _stale(world, "lives in Porto"), await _evidence(world)
    agent = _agent(world, [_tools(("update_fact", {"fact_id": fact, "updates": {"state": "current", "expiry": HORIZON},
                                                   "basis": _on(ev)})), REPORT])
    recorder = RecordingUpdates(world.store.adapter)
    agent._fact_updates = recorder                      # as the dry-run kit installs its recorder

    response = await _run(agent, await _task(world, fact))

    assert [fid for fid, _ in recorder.calls] == [fact]
    assert [d["outcome"] for d in response.result["outcomes"]] == ["revived"]


# ------------------------------------------------------------------------------------------------ the message

async def test_the_message_has_no_budget_and_asks_for_no_question_text(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [REPORT])

    await _run(agent, await _task(world, fact))

    text = _user_text(agent)
    assert text.startswith("SYSTEM MAINTENANCE — STALE FACT REVIEW")
    assert "Every listed fact gets exactly one outcome." in text
    # P2 revised (P2-7 checklist, outcome_order (3)): the history column replaces dated_plan_passed, and a passed date
    # is rewritten as history before any archive, which only the owner's reply backs
    assert ("<fact_id> | neighbour_says=<id|none> | reply=<yes|no|over|never|unclear|none> | "
            "history=<evidence|own_text|ask|none> | outcome=<…>") in text
    header = text.split("Facts under review:")[0]
    order = ["(1) a current fact states the same thing: supersede it by that fact",
             "(2) the reply or evidence says it still holds: revive it with that basis",
             "(3) its date has passed: rewrite it as history (basis evidence or reply, or date_passed when its own text "
             "already reports the outcome); otherwise, when the owner's reply says it is over: archive it; it was never "
             "true: invalidate it",
             "(4) anything else: ask_owner"]
    assert all(step in header for step in order)
    assert [header.index(step) for step in order] == sorted(header.index(step) for step in order)
    assert "keep it stale" not in header and "no call" not in header
    lowered = text.lower()
    assert "budget" not in lowered and "question text" not in lowered and "question_text" not in lowered
    assert "at most" not in lowered.split("facts under review:")[0]


async def test_fixed_mode_has_no_owner_messages_and_no_language_request(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [REPORT])

    await _run(agent, await _task(world, fact, mirror=False))

    text = _user_text(agent)
    assert "owner_messages" not in text and "message_language" not in text and "labels" not in text


async def test_fixed_mode_refuses_owner_messages_in_the_task(world):
    fact = await _stale(world, "plays tennis on Sundays")
    with pytest.raises(ValueError):
        await _task(world, fact, mirror=False, owner_messages=("привет",))


async def test_mirror_mode_carries_the_owners_messages_and_the_labels_instruction(world):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [REPORT])
    said = ("Привет, как дела?", "[System: reminder fired]", "Купил новую ракетку")

    await _run(agent, await _task(world, fact, mirror=True, owner_messages=said))

    text = _user_text(agent)
    block = next(l for l in text.splitlines() if l.startswith("owner_messages"))
    assert json.loads(block.split(": ", 1)[1]) == ["Привет, как дела?", "Купил новую ракетку"]
    assert "[System:" not in text
    assert "`message_language`" in text and "`labels`" in text
    assert json.dumps(dict(CANONICAL_LABELS), ensure_ascii=False) in text


async def test_each_fact_line_is_built_from_the_view_with_versions_neighbours_and_question(world):
    fact, near = await _stale(world, "plays tennis on Sundays"), await _evidence(world, "joined a tennis club")
    open_question = build_question(QuestionStatus.ASKED, fact_id=fact, account_id=ACC, user_id=USER,
                                   reply_text="still do", reply_at=NOW - timedelta(days=1))
    version = (await world.repo.get_fact_by_id(near)).model_copy(update={"version": 1, "text": "plays tennis", "valid_from": NOW - timedelta(days=90)})
    agent = _agent(world, [REPORT])

    await _run(agent, await _task(world, fact, neighbours=(near,), questions=(open_question,), versions=(version,)))

    line = _line(_user_text(agent))
    stored = await world.repo.get_fact_by_id(fact)
    assert list(line) == ["fact_id", "content", "domain", "type", "expiry", "due", "reported", "stale_since", "versions",
                          "neighbours", "question"]
    assert line["fact_id"] == fact and line["content"] == "plays tennis on Sundays"
    assert line["expiry"] == {"kind": "horizon", "days": 14} and line["type"] == stored.type.value
    assert line["reported"] == (NOW - timedelta(days=60)).date().isoformat()
    assert line["stale_since"] == "2026-10-23"
    assert line["versions"] == [{"version": 1, "content": "plays tennis", "valid_from": (NOW - timedelta(days=90)).date().isoformat()}]
    (neighbour,) = line["neighbours"]
    assert neighbour["fact_id"] == near and neighbour["content"] == "joined a tennis club"
    assert neighbour["reported"] == "2026-10-22"
    assert line["question"] == {  # P2 revised (C, _review_line): the question's kind follows its id
        "question_id": "q1", "kind": "still_true", "asked_on": open_question.asked_at.astimezone(ZoneInfo(TZ)).date().isoformat(),
        "fallback": "revive", "timed_out": False, "reply": "still do", "reply_at": "2026-10-22T08:30:00+01:00",
    }


# ------------------------------------------------------------------------------------------------ the report

async def test_a_report_with_a_language_and_labels_returns_both(world):
    fact = await _stale(world, "plays tennis on Sundays")
    assert set(RU_LABELS) == LABEL_KEYS
    final = LLMResponse(text="```json\n" + json.dumps({"operations": [], "message_language": "RU", "labels": RU_LABELS},
                                                      ensure_ascii=False) + "\n```", tool_calls=[])
    agent = _agent(world, [final])

    response = await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Привет",)))

    assert response.result["message_language"] == "ru"
    assert response.result["labels"] == RU_LABELS


@pytest.mark.parametrize("report", [
    "not json at all",
    json.dumps({"message_language": "Russian", "labels": {**RU_LABELS, "memory_option_never": "1"}}),
    json.dumps({"message_language": "ru-RU", "labels": "Да / Нет"}),
    json.dumps({"message_language": 7, "labels": {k: v for k, v in RU_LABELS.items() if k != "memory_reply_hint"}}),
    json.dumps(["ru"]),
])
async def test_a_malformed_report_returns_none(world, report):
    fact = await _stale(world, "plays tennis on Sundays")
    agent = _agent(world, [LLMResponse(text=report, tool_calls=[])])

    response = await _run(agent, await _task(world, fact, mirror=True, owner_messages=("Привет",)))

    assert response.status == AgentStatus.SUCCESS
    assert response.result["message_language"] is None and response.result["labels"] is None
