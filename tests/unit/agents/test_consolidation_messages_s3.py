"""The consolidator's code-built messages (Facts S3, T6): Smart's timestamp in the owner's calendar, the Today line on
every pass, and what Stage 2a, Stage 2b and the email pass tell the model. Real passes, adapter and write service on the
evaluating fake store under a scripted LLM; the store is read back, never mocked."""
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.email import IndexedEmail
from src.domain.entities import FactDomain
from src.domain.fact_write_context import as_utc
from src.domain.llm import LLMResponse, Message, MessagePart, ToolCall
from src.domain.local_time import local_day, local_stamp
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon, on

REPORT = LLMResponse(text='{"operations": []}')
PARIS = "Europe/Paris"
STAGE1 = "Begin deliberate consolidation."
CLUSTER = "FACT CLUSTER REVIEW"
RULEBOOK = "STANDING DIRECTIVES REVIEW"
ATTRS = {"domain": "biographical", "temporal_class": "stable", "context_priority": "medium", "tags": ["t"],
         "metadata": {}, "type": "event", "expiry": {"kind": "none"}}


def utc(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def create(content: str, **over: Any) -> ToolCall:
    return ToolCall(name="create_fact", args={"content": content, "fact_attributes": {**ATTRS, **over}})


def calls(*tool_calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=list(tool_calls))


def by_text(store: FactStore, text: str) -> Dict[str, Any]:
    (doc,) = [d for d in store.docs.values() if d["text"] == text]
    return doc


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def make_agent(store: FactStore, script: Callable[[str], LLMResponse], *, email_repo: Optional[Any] = None):
    """An agent whose LLM answers `script(first_message_text)`; every first message is kept in `agent.firsts`."""
    port = FactManagementAdapter(
        store.repo, store.embedder, store.write, SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    )
    pb = MagicMock()
    pb.build_for_agent = AsyncMock(return_value="SYSTEM")
    pb.invalidate_biographical_cache = MagicMock()
    execution = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=execution,
        repository=store.repo, embedding_service=store.embedder, fact_write_service=MagicMock(),
        fact_management_port=port, fact_updates=port,  # reviewer edit (S4 T6b): the adapter is both ports
        prompt_builder=pb, indexed_email_repo=email_repo,
    )
    agent.INLINE_CLUSTER_REVIEW = False
    agent.firsts = []
    agent.pb = pb
    store.repo.refresh_biographical_context_cache = AsyncMock()

    async def call(request, turn=1):
        first = request.messages[0].parts[0].text
        if len(request.messages) == 1:
            agent.firsts.append(first)
        return script(first)

    agent._call_llm = call
    return agent


def once(per_pass: Dict[str, LLMResponse]) -> Callable[[str], LLMResponse]:
    spent: set = set()

    def script(first: str) -> LLMResponse:
        for marker, response in per_pass.items():
            if marker in first and marker not in spent:
                spent.add(marker)
                return response
        return REPORT

    return script


def message(task: str, payload: Dict[str, Any], zone: Optional[str] = PARIS) -> AgentMessage:
    context: Dict[str, Any] = {"user_id": USER}
    if zone is not None:
        context["owner_timezone"] = zone
    return AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                        payload={"task": task, **payload}, context=context)


def pass_text(agent: ConsolidationAgent, marker: str) -> str:
    (text,) = [t for t in agent.firsts if marker in t]
    return text


def entries(text: str) -> List[Dict[str, Any]]:
    """The numbered JSON entries of a Stage 2a / 2b message."""
    return [json.loads(line.split(". ", 1)[1]) for line in text.splitlines() if re.match(r"^\d+\. \{", line)]


def days_around_now(zone: str) -> set:
    return {local_day(datetime.now(timezone.utc) + delta, zone) for delta in (timedelta(minutes=-1), timedelta(minutes=1))}


# ----------------------------------------------------------------------------------------------- the Today line


def email(email_id: str, when: datetime) -> IndexedEmail:
    return IndexedEmail(
        email_id=email_id, user_id=USER, account_id=ACC, source="gmail", text=f"mail {email_id}", tags=["mail"],
        category="travel", metadata={"subject": email_id}, subject=email_id, from_address="a@example.com",
        email_date=when, indexed_at=when,
    )


class OneBatchOfEmails:
    def __init__(self, *dates: datetime) -> None:
        self.batch = [email(f"e{i}", when) for i, when in enumerate(dates or (datetime(2026, 10, 10, 9),), 1)]

    async def get_unconsolidated_batch(self, user_id: str, limit: int = 10) -> List[IndexedEmail]:
        return self.batch

    async def mark_consolidated(self, user_id: str, ids: List[str], when: datetime) -> None:
        self.batch = []


async def test_every_pass_message_ends_with_the_today_line_in_the_owner_zone(store):
    put_raw(store, "d-1", "never use emojis", entity={"domain": FactDomain.AGENT_DIRECTIVE})
    agent = make_agent(store, once({STAGE1: calls(create("seed fact about Rome"))}), email_repo=OneBatchOfEmails())
    agent.INLINE_CLUSTER_REVIEW = True
    batch = [{"role": "user", "text": "hello", "created_at": utc(20, 8).timestamp()}]
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(batch, USER, ACC, [], owner_timezone=PARIS)
        await agent._handle_consolidate_cluster(
            message("consolidate_cluster", {"cluster": [{"fact_id": "x", "content": "c", "similarity": 0.5}]}), USER, ACC, [], 0.0)
        await agent._handle_consolidate_email(message("consolidate_email", {"number_of_batches": 1}), USER, ACC, [], 0.0)

    markers = {"stage1": STAGE1, "stage2a": CLUSTER, "stage2b": RULEBOOK, "email": "[system_alert]"}
    assert len(agent.firsts) == 5       # stage 1, 2a, 2b, the cluster pass, the email pass
    pattern = re.compile(r"\n\nToday: (\d{4}-\d{2}-\d{2} \w{3}) \(Europe/Paris, the owner's timezone\)\.$")
    for text in agent.firsts:
        found = pattern.search(text)
        assert found, text[-120:]
        assert found.group(1) in days_around_now(PARIS)
    assert all(any(marker in t for t in agent.firsts) for marker in markers.values())


async def test_without_an_owner_zone_the_today_line_names_utc(store):
    agent = make_agent(store, lambda first: REPORT)
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._handle_consolidate_cluster(
            message("consolidate_cluster", {"cluster": [{"fact_id": "x", "content": "c", "similarity": 0.5}]}, zone=None),
            USER, ACC, [], 0.0)
    found = re.search(r"Today: (\d{4}-\d{2}-\d{2} \w{3}) \(UTC, the owner's timezone\)\.$", agent.firsts[0])
    assert found and found.group(1) in days_around_now("UTC")


# ----------------------------------------------------------------------------------------------- conversation stamps


def smart_prefix(created_at: float, zone: str) -> str:
    """What Smart's own `_inject_timestamps` puts before the same message in the same zone."""
    speaker = ConsolidationAgent.__new__(ConsolidationAgent)
    speaker._user_timezone = zone
    (stamped,) = BaseAgent._inject_timestamps(speaker, [Message(role="user", parts=[MessagePart(text="hi")], created_at=created_at)])
    return stamped.parts[0].text.removesuffix(" hi").strip("[]")


def conversation(msgs: List[Dict[str, Any]], zone: str) -> List[Dict[str, Any]]:
    agent = ConsolidationAgent.__new__(ConsolidationAgent)
    return agent._prepare_structured_conversation(msgs, zone)


@pytest.mark.parametrize("zone", [PARIS, "UTC", "America/New_York", "Asia/Kolkata", "not a zone"])
@pytest.mark.parametrize("moment", [utc(22, 10), utc(1, 0, 30, month=11), utc(25, 0, 59, month=3), utc(1, 23, 59, month=1)])
def test_the_conversation_stamp_is_smarts_stamp_for_the_same_message_and_zone(zone, moment):
    (line,) = conversation([{"role": "user", "text": "hi", "created_at": moment.timestamp()}], zone)
    assert line["timestamp"] == smart_prefix(moment.timestamp(), zone)
    assert line["timestamp"] == local_stamp(moment, zone)


def test_the_stamp_has_the_owners_year_weekday_and_zone():
    (line,) = conversation([{"role": "user", "text": "hi", "created_at": "2026-10-22T10:00:00Z"}], PARIS)
    assert line["timestamp"] == "2026-10-22 Thu 12:00 CEST"


@pytest.mark.parametrize("value", [
    "2026-10-22T10:00:00",              # naive ISO: UTC
    "2026-10-22T10:00:00Z",
    "2026-10-22T12:00:00+02:00",
    utc(22, 10).timestamp(),
    utc(22, 10),
])
def test_every_readable_time_form_renders_the_same_moment(value):
    (line,) = conversation([{"role": "user", "text": "hi", "created_at": value}], PARIS)
    assert line["timestamp"] == "2026-10-22 Thu 12:00 CEST"


@pytest.mark.parametrize("msg", [
    {"role": "user", "text": "hi", "timestamp": "INVALID"},
    {"role": "user", "text": "hi", "created_at": "last Tuesday"},
    {"role": "user", "text": "hi", "created_at": {}},
    {"role": "user", "text": "hi", "created_at": "", "timestamp": ""},
    {"role": "user", "text": "hi"},
])
def test_an_unreadable_or_missing_time_renders_empty_and_never_raises(msg):
    (line,) = conversation([msg], PARIS)
    assert line["timestamp"] == ""


def test_the_stamp_and_the_span_read_the_same_time_of_a_message():
    both = {"role": "user", "text": "a", "created_at": utc(20, 8).timestamp(), "timestamp": utc(21, 8).timestamp()}
    bad_first = {"role": "user", "text": "b", "created_at": "soon", "timestamp": utc(22, 8).timestamp()}
    empty_first = {"role": "user", "text": "c", "created_at": "", "timestamp": utc(23, 8).timestamp()}
    only_timestamp = {"role": "user", "text": "d", "timestamp": utc(24, 8).timestamp()}
    msgs = [both, bad_first, empty_first, only_timestamp]
    stamps = [line["timestamp"] for line in conversation(msgs, "UTC")]
    seen = [as_utc(ConsolidationAgent._message_time(m)) for m in msgs]
    assert seen == [utc(20, 8), utc(22, 8), utc(23, 8), utc(24, 8)]
    assert stamps == [local_stamp(moment, "UTC") for moment in seen]
    assert ConsolidationAgent._times_span([ConsolidationAgent._message_time(m) for m in msgs], "message times") == (utc(20, 8), utc(24, 8))


async def test_the_deliberate_process_shows_the_model_the_owners_stamps(store):
    agent = make_agent(store, lambda first: REPORT)
    batch = [{"role": "user", "text": "I fly on Friday", "created_at": "2026-10-22T10:00:00Z"}]
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(batch, USER, ACC, [], owner_timezone=PARIS)
    history = agent.pb.build_for_agent.call_args_list[0].kwargs["conversation_history"]
    assert history == [{"role": "user", "content": "I fly on Friday", "timestamp": "2026-10-22 Thu 12:00 CEST"}]


async def test_the_legacy_v2_path_gets_the_owners_zone_too(store):
    agent = make_agent(store, lambda first: REPORT)
    agent.prompt_version = "v2"
    batch = [{"role": "user", "text": "I fly on Friday", "created_at": "2026-10-22T10:00:00Z"}]
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate(message("consolidate", {"messages": batch}), USER, ACC, [], 0.0)
    assert response.status.value == "success", response
    history = agent.pb.build_for_agent.call_args_list[0].kwargs["conversation_history"]
    assert history[0]["timestamp"] == "2026-10-22 Thu 12:00 CEST"


async def test_the_legacy_v2_path_without_a_zone_in_the_message_shows_utc(store):
    agent = make_agent(store, lambda first: REPORT)
    agent.prompt_version = "v2"
    batch = [{"role": "user", "text": "I fly on Friday", "created_at": "2026-10-22T10:00:00Z"}]
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._handle_consolidate(message("consolidate", {"messages": batch}, zone=None), USER, ACC, [], 0.0)
    assert agent.pb.build_for_agent.call_args_list[0].kwargs["conversation_history"][0]["timestamp"] == "2026-10-22 Thu 10:00 UTC"


# ----------------------------------------------------------------------------------------------- Stage 2a entries


async def seed_and_search(store: FactStore, agent: ConsolidationAgent) -> List[Dict[str, Any]]:
    """Three facts written through the real tool (a date, a horizon, a history fact) plus a legacy document with no decision."""
    reported = "2026-10-22T23:30:00+00:00"                       # 23 October 01:30 in Paris
    with RequestContext(user_id=USER, account_id=ACC):
        responses = await agent._execute_fact_management_tools(
            [create("trip to Rome in January", expiry=on("2027-01-15"), reported_date=reported),
             create("takes the pills daily", expiry=horizon(30), reported_date=reported),
             create("was 80 kg in March 2024")],
            USER, ACC, write_context=ctx())
    assert [json.loads(r.result_str)["status"] for r in responses] == ["created"] * 3, [r.result_str for r in responses]
    put_raw(store, "legacy", "an old note without a decision")
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent._fact_management.search_existing_facts(
            keywords=["trip", "pills", "weight", "note"], primary_query="trip to Rome pills 80 kg old note", limit=20, timezone=PARIS)


async def test_stage2a_entries_carry_expiry_due_and_the_owners_reported_day(store):
    agent = make_agent(store, lambda first: REPORT)
    found = await seed_and_search(store, agent)
    shown = {e["content"]: e for e in entries(ConsolidationAgent._build_cluster_message(found))}

    trip, pills = shown["trip to Rome in January"], shown["takes the pills daily"]
    assert trip["expiry"] == {"kind": "date", "date": "2027-01-15"} and "due" not in trip
    assert pills["expiry"] == {"kind": "horizon", "days": 30}
    assert pills["due"] == local_stamp(utc(22, 23, 30) + timedelta(days=30), PARIS)[:10]
    assert trip["reported"] == pills["reported"] == "2026-10-23"                 # the owner's calendar day, not the UTC day
    assert shown["was 80 kg in March 2024"]["expiry"] == {"kind": "none"}
    legacy = shown["an old note without a decision"]
    assert legacy["expiry"] is None and "due" not in legacy                      # classified separately, shown as null


def test_the_builder_still_accepts_entries_without_the_new_keys():
    (entry,) = entries(ConsolidationAgent._build_cluster_message([{"fact_id": "f1", "content": "plain", "similarity": 0.9}]))
    assert entry == {"fact_id": "f1", "content": "plain", "similarity": 0.9, "expiry": None, "reported": None}


@pytest.mark.parametrize("given,shown", [("not a date", None), ("", None), (None, None), ("2026-10-23T01:30:00+02:00", "2026-10-23")])
def test_an_unreadable_reported_date_is_shown_as_null(given, shown):
    (entry,) = entries(ConsolidationAgent._build_cluster_message(
        [{"fact_id": "f1", "content": "plain", "similarity": 0.9, "reported_date": given}]))
    assert entry["reported"] == shown


async def test_a_date_entry_sent_back_unchanged_in_a_content_update_keeps_expires_at(store):
    agent = make_agent(store, lambda first: REPORT)
    found = await seed_and_search(store, agent)
    trip = next(e for e in entries(ConsolidationAgent._build_cluster_message(found)) if e["content"].startswith("trip"))
    before = by_text(store, "trip to Rome in January")
    with RequestContext(user_id=USER, account_id=ACC):
        responses = await agent._execute_fact_management_tools(
            [ToolCall(name="update_fact", args={"fact_id": trip["fact_id"], "updates": {
                "content": "trip to Rome in January 2027", "expiry": trip["expiry"]}})],
            USER, ACC, write_context=ctx("stage2a"))
    assert json.loads(responses[0].result_str)["status"] == "updated", responses[0].result_str
    after = by_text(store, "trip to Rome in January 2027")
    assert after["expires_at"] == before["expires_at"] and after["expiry_kind"] == "date"


async def test_the_cluster_passs_auto_fetched_facts_show_the_same_expiry_as_the_search_dict(store):
    agent = make_agent(store, lambda first: REPORT)
    found = await seed_and_search(store, agent)
    from_search = {e["content"]: e for e in entries(ConsolidationAgent._build_cluster_message(found))}

    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate_cluster(message("consolidate_cluster", {"limit": 10}), USER, ACC, [], 0.0)
    assert response.status.value == "success", response
    fetched = {e["content"]: e for e in entries(pass_text(agent, CLUSTER))}
    assert set(fetched) >= {"trip to Rome in January", "takes the pills daily", "was 80 kg in March 2024"}
    for content in ("trip to Rome in January", "takes the pills daily", "was 80 kg in March 2024"):
        for key in ("expiry", "reported"):
            assert fetched[content][key] == from_search[content][key], (content, key)
        assert fetched[content].get("due") == from_search[content].get("due"), content
    assert fetched["trip to Rome in January"]["expiry"] == {"kind": "date", "date": "2027-01-15"}
    assert fetched["an old note without a decision"]["expiry"] is None


# ----------------------------------------------------------------------------------------------- the texts


def test_stage2a_text_says_how_horizons_count_and_when_to_send_expiry():
    text = " ".join(ConsolidationAgent._build_cluster_message([]).split())
    assert "A horizon counts from `reported`; this pass cannot change `reported`" in text
    assert "Give `expiry` only on a create, a merge or a rewrite you make in this pass" in text
    assert "do not send `expiry` for a fact you do not otherwise change" in text
    assert "facts with `expiry: null` are classified separately" in text


def test_stage2a_text_asks_for_derived_from_on_every_split():
    text = " ".join(ConsolidationAgent._build_cluster_message([]).split())
    assert 'give each create_fact `derived_from: ["<the split fact\'s id>"]`' in text
    assert "a merge takes its date from its sources by itself" in text


def test_stage2b_text_names_fact_attributes_not_metadata():
    text = ConsolidationAgent._build_directive_review_message([], 15)
    assert "metadata=" not in text
    assert text.count("fact_attributes={") == 2          # the DEMOTE create and the MERGE


def test_stage2b_merge_pins_expiry_to_none():
    text = ConsolidationAgent._build_directive_review_message([], 15)
    merge = text[text.index("  MERGE"):text.index("A clean rulebook")]
    assert 'expiry: {kind: "none"}' in merge and "pinned to none" in merge


def test_stage2b_demotion_asks_for_expiry_and_derived_from():
    text = ConsolidationAgent._build_directive_review_message([], 15)
    demote = text[text.index("  DEMOTE"):text.index("  INVALIDATE")]
    assert "expiry your schema requires" in demote and "decide the" in demote
    assert 'derived_from: ["<this directive\'s fact_id>"]' in demote
    assert 'update_fact(<fact_id>, {state: "invalidated"})' in demote          # the state-only closure is unchanged


async def test_the_email_alert_dates_by_the_candidate_and_calls_a_passed_date_history(store):
    agent = make_agent(store, lambda first: REPORT, email_repo=OneBatchOfEmails())
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._handle_consolidate_email(message("consolidate_email", {"number_of_batches": 1}), USER, ACC, [], 0.0)
    text = pass_text(agent, "[system_alert]")
    assert "Copy the candidate's `date`, as given, into reported_date." in text
    assert "Information whose date or horizon has already passed is history or noise." in text
    assert "Candidates:\n1. {" in text


# ----------------------------------------------------------------------------------------------- the stamp as reported_date


async def test_a_stage1_create_may_send_the_stamp_the_conversation_showed(store):
    stamp = "2026-10-20 Tue 13:00 CEST"
    batch = [{"role": "user", "text": "x", "created_at": utc(20, 8).timestamp()},
             {"role": "user", "text": "y", "created_at": utc(20, 11, 30).timestamp()}]
    agent = make_agent(store, once({STAGE1: calls(
        create("said at one", reported_date=stamp),
        create("wrong zone name", reported_date="2026-10-20 Tue 13:00 EST"),
    )}))
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(batch, USER, ACC, [], owner_timezone=PARIS)
    assert as_utc(by_text(store, "said at one")["reported_date"]) == utc(20, 11)       # 13:00 CEST
    assert "wrong zone name" not in {d["text"] for d in store.docs.values()}          # refused as before: not the zone's stamp


async def test_the_stamp_is_refused_where_the_zone_is_not_known(store):
    from src.domain.fact_argument_parsing import parse_reported_date
    with pytest.raises(ValueError, match="reported_date"):
        parse_reported_date("2026-10-20 Tue 13:00 CEST")
    assert parse_reported_date("2026-10-20 Tue 13:00 CEST", PARIS) == utc(20, 11)


# ----------------------------------------------------------------------------------------------- email dates, update and merge


async def test_an_email_candidates_date_is_the_owners_moment_and_copying_it_dates_the_fact_exactly(store):
    sent = [datetime(2026, 10, 22, 10, 0), datetime(2026, 10, 22, 23, 30)]         # naive = UTC; the second is 23 Oct 01:30 in Paris

    def script(first: str) -> LLMResponse:
        if "[system_alert]" not in first or "Copy the candidate" not in first or script.done:
            return REPORT
        script.done = True
        candidates = [json.loads(line.split(". ", 1)[1]) for line in first.split("Candidates:\n", 1)[1].splitlines()
                      if re.match(r"^\d+\. \{", line)]
        return calls(*[create(f"fact from {c['email_id']}", reported_date=c["date"]) for c in candidates])

    script.done = False
    agent = make_agent(store, script, email_repo=OneBatchOfEmails(*sent))
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._handle_consolidate_email(message("consolidate_email", {"number_of_batches": 1}), USER, ACC, [], 0.0)

    shown = [json.loads(line.split(". ", 1)[1])["date"] for line in agent.firsts[0].split("Candidates:\n", 1)[1].splitlines()
             if re.match(r"^\d+\. \{", line)]
    assert shown == ["2026-10-22T12:00:00+02:00", "2026-10-23T01:30:00+02:00"]      # the owner's calendar day, not the UTC day
    assert as_utc(by_text(store, "fact from e1")["reported_date"]) == utc(22, 10)
    assert as_utc(by_text(store, "fact from e2")["reported_date"]) == utc(22, 23, 30)    # the exact moment, not midnight clamped


def test_the_candidate_date_is_utc_without_an_owner_zone_and_a_naive_date_is_utc():
    emails = [email("e1", datetime(2026, 10, 22, 23, 30))]
    (line,) = ConsolidationAgent._format_email_candidates(emails, "UTC").splitlines()
    assert json.loads(line.split(". ", 1)[1])["date"] == "2026-10-22T23:30:00+00:00"


STAMP = "2026-10-20 Tue 13:00 CEST"                     # 11:00 UTC
STAGE1_BATCH = [{"role": "user", "text": "x", "created_at": utc(20, 8).timestamp()},
                {"role": "user", "text": "y", "created_at": utc(20, 11, 30).timestamp()}]


async def seed_two(agent: ConsolidationAgent) -> List[str]:
    with RequestContext(user_id=USER, account_id=ACC):
        responses = await agent._execute_fact_management_tools(
            [create("walks daily", reported_date="2026-10-10T00:00:00+00:00"),
             create("walks far", reported_date="2026-10-10T00:00:00+00:00")], USER, ACC, write_context=ctx())
    return [json.loads(r.result_str)["fact_id"] for r in responses]


async def test_a_stage1_update_may_send_the_stamp_the_conversation_showed(store):
    agent = make_agent(store, lambda first: REPORT)
    walks, _ = await seed_two(agent)
    agent._call_llm = make_agent(store, once({STAGE1: calls(
        ToolCall(name="update_fact", args={"fact_id": walks, "updates": {"reported_date": STAMP}}))}))._call_llm
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(STAGE1_BATCH, USER, ACC, [], owner_timezone=PARIS)
    assert as_utc(store.docs[walks]["reported_date"]) == utc(20, 11)


async def test_a_stage1_merge_may_send_the_stamp_the_conversation_showed(store):
    agent = make_agent(store, lambda first: REPORT)
    walks, far = await seed_two(agent)
    agent._call_llm = make_agent(store, once({STAGE1: calls(ToolCall(name="merge_facts", args={
        "fact_ids": [walks, far], "merged_content": "walks a lot",
        "fact_attributes": {**ATTRS, "reported_date": STAMP}}))}))._call_llm
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(STAGE1_BATCH, USER, ACC, [], owner_timezone=PARIS)
    assert as_utc(by_text(store, "walks a lot")["reported_date"]) == utc(20, 11)
