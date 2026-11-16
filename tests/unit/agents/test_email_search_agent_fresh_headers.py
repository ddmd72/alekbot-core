"""EmailSearchAgent.search_emails - fresh headers (mail newer than the index) and exact keys, decided by code."""
import json
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock


from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.email_search_agent import EmailSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.email import EmailMetadata
from src.domain.email_open_texts import LIVE_NOTE_NOT_CONNECTED, LIVE_NOTE_UNAVAILABLE
from src.domain.email_search_render import MODE_NOTHING, MODE_RANKED, MailSearchResult
from src.domain.exceptions import EmailNotConnected
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMResponse, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from tests.unit.domain.email_search_fixtures import school_news

UTC = timezone.utc
NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _hit(email_id: str) -> dict:
    return {"email_id": email_id, "from": "clara@example.com", "date": "2026-02-10",
            "text": "Example School parents' meeting", "attachments": []}


def _meta(email_id: str, subject: str = "Invoice FA-0137", date: datetime = datetime(2026, 3, 4, 9, 0)) -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject=subject, from_address="shop@example.com",
                         date=date, labels=[], snippet="first line")


class FakeService:
    """Scripted EmailSearchService: records every live listing; `listing` answers by key (None = the fresh one)."""

    def __init__(self, index: Optional[List[dict]] = None, watermark: Optional[datetime] = None):
        self.index = index or []
        self.watermark = watermark
        self.listing: Dict[Optional[str], object] = {}
        self.calls: List[dict] = []


    async def index_watermark(self, user_id: str) -> Optional[datetime]:
        return self.watermark

    async def list_headers(self, user_id, *, after=None, before=None, key=None, limit=100):
        self.calls.append({"after": after, "before": before, "key": key, "limit": limit})
        answer = self.listing.get(key, ([], False))
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    def fresh_calls(self) -> List[dict]:
        return [c for c in self.calls if c["key"] is None]

    def key_calls(self) -> List[dict]:
        return [c for c in self.calls if c["key"] is not None]


class FakeCardSearch:
    """The card search: answers with the service's `index` hits as ranked cards (a card per hit id), or nothing."""

    def __init__(self, service: FakeService):
        self.service = service

    async def search(self, user_id, account_id, request):
        cards = tuple(school_news(h["email_id"], h["text"], date(2026, 2, 10)) for h in self.service.index)
        ids = tuple(c.mail.email_id for c in cards)
        return MailSearchResult(text="", mode=MODE_RANKED if cards else MODE_NOTHING, shown_ids=ids, folded_ids={},
                                ranked_ids=ids, total=len(ids), sections=(), shown=cards)


def _slots_of(extraction: dict) -> dict:
    """The v1 extraction's period and exact keys, as the slots call's answer."""
    start, end = extraction.get("date_from"), extraction.get("date_to")
    time = ({"intent": "period", "from": start or "", "to": end or ""} if start or end
            else {"intent": "none", "from": "", "to": ""})
    keys = extraction.get("exact_keys")
    return {"subject": "", "doc_types": [], "domains": [], "people": [], "time": time,
            "exact_keys": keys if isinstance(keys, list) else [], "scope": "top"}


def _agent(service: FakeService, extraction: dict, tz: str = "UTC", request: Optional[str] = None) -> EmailSearchAgent:
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = LLMResponse(text=json.dumps(_slots_of(extraction)), tool_calls=[])
    builder = AsyncMock(spec=PromptBuilderPort)
    builder.build_for_agent.return_value = "prompt"
    context = AgentExecutionContext(agent_type="email_search", provider=llm, model_name="m", tier=PerformanceTier.ECO,
                                    capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience())
    agent = EmailSearchAgent(
        config=AgentConfig(agent_id="email_search_agent_user123", agent_type="email_search"),
        execution_context=context, prompt_builder=builder, email_search_service=service,  # type: ignore[arg-type]
        card_search=FakeCardSearch(service),  # type: ignore[arg-type]
        user_id="user123", user_timezone=tz, clock=lambda: NOW,
    )
    # the slots keep only the keys the request text holds, and a window filters only when the text names it
    days = [d for d in (extraction.get("date_from"), extraction.get("date_to")) if d]   # a window filters only when named
    agent.test_request = request or " ".join(  # type: ignore[attr-defined]
        ["mail", *days, *[k for k in _slots_of(extraction)["exact_keys"] if isinstance(k, str)]])
    return agent


async def _search(agent: EmailSearchAgent) -> Tuple[object, dict]:
    msg = AgentMessage(intent=AgentIntent.QUERY, payload={"query": agent.test_request, "history": []}, sender="s",  # type: ignore[attr-defined]
                       recipient="r", task_id="t", context={"user_id": "user123", "account_id": "acc1"})
    response = await agent.execute(msg)
    assert response.status == AgentStatus.SUCCESS
    return response, json.loads(response.result)


# ----------------------------------------------------------------- the window

async def test_an_old_watermark_opens_the_window_at_the_watermark():
    service = FakeService(watermark=datetime(2026, 3, 1, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {}))
    assert service.fresh_calls() == [{"after": datetime(2026, 3, 1, 6, 0, tzinfo=UTC), "before": None,
                                      "key": None, "limit": 100}]


async def test_a_recent_watermark_opens_the_window_at_the_start_of_yesterday():
    service = FakeService(watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {}))
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


async def test_the_start_of_yesterday_is_the_users_local_midnight():
    service = FakeService(watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {}, tz="America/New_York"))  # 07:00 on 4 March there; yesterday began 05:00 UTC
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 3, 5, 0, tzinfo=UTC)


async def test_an_unknown_timezone_falls_back_to_utc():
    service = FakeService(watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {}, tz="Mars/Olympus"))
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


async def test_a_never_built_index_has_no_lower_bound():
    service = FakeService(watermark=None)
    _, body = await _search(_agent(service, {}))
    assert service.fresh_calls()[0]["after"] is None
    assert body["fresh"]["since"] is None


async def test_a_request_ending_before_the_window_skips_the_listing():
    service = FakeService(watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    _, body = await _search(_agent(service, {"date_from": "2026-01-01", "date_to": "2026-02-28"}))
    assert service.fresh_calls() == []
    assert body["fresh"] is None and body["index"]["mode"] == MODE_NOTHING  # nothing in the index either


async def test_a_skipped_listing_is_null_in_the_result():
    service = FakeService(index=[_hit("e1")], watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    _, body = await _search(_agent(service, {"date_from": "2026-01-01", "date_to": "2026-02-28"}))
    assert body["fresh"] is None and body["index"]["shown_ids"] == ["e1"]


async def test_a_requested_start_narrows_after():
    service = FakeService(watermark=datetime(2026, 3, 1, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {"date_from": "2026-03-03"}))
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


async def test_a_requested_start_before_the_window_does_not_widen_it():
    service = FakeService(watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    # the text names a time but no day: a named day would be the window itself (code beats the slot on a named period)
    await _search(_agent(service, {"date_from": "2026-01-01"}, request="mail since the new year"))
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)


async def test_a_requested_start_without_a_watermark_is_the_bound():
    service = FakeService(watermark=None)
    await _search(_agent(service, {"date_from": "2026-03-02"}))
    assert service.fresh_calls()[0]["after"] == datetime(2026, 3, 2, 0, 0, tzinfo=UTC)


async def test_a_past_end_date_sets_before_at_the_end_of_that_local_day():
    service = FakeService(watermark=datetime(2026, 2, 20, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {"date_to": "2026-03-02"}, tz="America/New_York"))
    before = service.fresh_calls()[0]["before"]
    assert before.astimezone(UTC) == datetime(2026, 3, 3, 4, 59, 59, 999999, tzinfo=UTC)


async def test_an_end_date_in_the_future_leaves_before_open():
    service = FakeService(watermark=datetime(2026, 3, 1, 6, 0, tzinfo=UTC))
    await _search(_agent(service, {"date_to": "2026-03-31"}))
    assert service.fresh_calls()[0]["before"] is None


# ----------------------------------------------------------------- exact keys

async def test_keys_are_cleaned_deduplicated_and_capped_at_three():
    service = FakeService()
    raw = [" FA-0137 ", "", "ab", "FA-0137", "HW-55812", 7, "clara@example.com", "789-012-3456789"]
    await _search(_agent(service, {"exact_keys": raw}))
    assert [c["key"] for c in service.key_calls()] == ["FA-0137", "HW-55812", "clara@example.com"]
    assert all(c["limit"] == 20 and c["after"] is None and c["before"] is None for c in service.key_calls())


async def test_a_non_list_keys_field_is_no_keys():
    service = FakeService()
    await _search(_agent(service, {"exact_keys": "FA-0137"}))
    assert service.key_calls() == []


# ----------------------------------------------------------------- the result

async def test_result_carries_index_fresh_and_key_groups():
    service = FakeService(index=[_hit("e1")], watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    service.listing[None] = ([_meta("m1", "Flight booking AAA→BBB")], True)
    service.listing["FA-0137"] = ([_meta("m2")], False)

    _, body = await _search(_agent(service, {"exact_keys": ["FA-0137"]}, tz="America/New_York"))

    assert body["index"]["shown_ids"] == ["e1"]
    assert body["fresh"] == {
        "since": "2026-03-03 00:00", "until": None, "listed": 1, "tz": "America/New_York", "truncated": True,
        "emails": [{"email_id": "m1", "from": "shop@example.com", "subject": "Flight booking AAA→BBB",
                    "date": "2026-03-04 04:00", "snippet": "first line"}],
    }
    assert body["by_key"] == [{"key": "FA-0137", "truncated": False, "listed": 1, "emails": [
        {"email_id": "m2", "from": "shop@example.com", "subject": "Invoice FA-0137",
         "date": "2026-03-04 04:00", "snippet": "first line"}]}]
    assert body["notes"] == []


async def test_ids_already_shown_are_dropped_from_later_lists():
    service = FakeService(index=[_hit("e1")], watermark=datetime(2026, 3, 4, 6, 0, tzinfo=UTC))
    service.listing[None] = ([_meta("e1"), _meta("m1")], False)
    service.listing["FA-0137"] = ([_meta("e1"), _meta("m1"), _meta("m2")], False)

    _, body = await _search(_agent(service, {"exact_keys": ["FA-0137"]}))

    assert [e["email_id"] for e in body["fresh"]["emails"]] == ["m1"]
    assert [e["email_id"] for e in body["by_key"][0]["emails"]] == ["m2"]


async def test_not_connected_adds_the_note_and_keeps_the_index_results():
    service = FakeService(index=[_hit("e1")], watermark=None)
    service.listing[None] = EmailNotConnected("user123")
    service.listing["FA-0137"] = EmailNotConnected("user123")

    _, body = await _search(_agent(service, {"exact_keys": ["FA-0137"]}))

    assert body["index"]["shown_ids"] == ["e1"]
    assert body["fresh"] is None and body["by_key"] == []
    assert body["notes"] == [LIVE_NOTE_NOT_CONNECTED]


async def test_any_other_listing_failure_is_a_note_not_a_failure():
    service = FakeService(index=[_hit("e1")], watermark=None)
    service.listing[None] = RuntimeError("boom")

    _, body = await _search(_agent(service, {}))

    assert body["index"]["shown_ids"] == ["e1"]
    assert body["notes"] == [LIVE_NOTE_UNAVAILABLE]


async def test_a_failed_key_listing_leaves_the_other_groups():
    service = FakeService(watermark=None)
    service.listing["FA-0137"] = RuntimeError("boom")
    service.listing["HW-55812"] = ([_meta("m2")], False)

    _, body = await _search(_agent(service, {"exact_keys": ["FA-0137", "HW-55812"]}))

    assert [g["key"] for g in body["by_key"]] == ["HW-55812"]
    assert body["notes"] == [LIVE_NOTE_UNAVAILABLE]


async def test_everything_empty_without_notes_is_a_nothing_result():
    # v1 answered a plain "No emails found" string; v2 always sends the index block, whose nothing section the formatter
    # prints as the final answer for the index, next to the empty live lists (email index v2 §5.4)
    service = FakeService(watermark=None)
    response, body = await _search(_agent(service, {"exact_keys": ["FA-0137"]}))
    assert body["index"]["mode"] == MODE_NOTHING and body["index"]["shown_ids"] == []
    assert body["fresh"] == {"since": None, "until": None, "listed": 0, "tz": "UTC", "truncated": False, "emails": []}
    assert body["by_key"] == [{"key": "FA-0137", "truncated": False, "listed": 0, "emails": []}]
    assert body["notes"] == [] and response.history_context is None


async def test_an_empty_fresh_listing_with_an_index_hit_still_reports_the_listing():
    service = FakeService(index=[_hit("e1")], watermark=None)
    _, body = await _search(_agent(service, {}))
    assert body["fresh"] == {"since": None, "until": None, "listed": 0, "tz": "UTC", "truncated": False, "emails": []}


# ----------------------------------------------------------------- history

async def test_history_context_lists_fresh_and_key_headers_by_subject():
    service = FakeService(index=[_hit("e1")], watermark=None)
    service.listing[None] = ([_meta("m1", "Flight booking AAA→BBB")], False)
    service.listing["FA-0137"] = ([_meta("m2", "x" * 300)], False)

    response, _ = await _search(_agent(service, {"exact_keys": ["FA-0137"]}))

    received = response.history_context["email_search_context"]["you_received"]
    assert [r["id"] for r in received] == ["e1", "m1", "m2"]
    assert received[1] == {"id": "m1", "from": "shop@example.com", "date": "2026-03-04 09:00",
                           "summary": "Flight booking AAA→BBB"}
    assert len(received[2]["summary"]) == 200


async def test_history_context_exists_for_a_headers_only_result():
    service = FakeService(watermark=None)
    service.listing[None] = ([_meta("m1")], False)
    response, _ = await _search(_agent(service, {}))
    assert response.history_context["email_search_context"]["you_received"][0]["id"] == "m1"
