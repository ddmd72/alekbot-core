"""EmailSearchAgent.search_emails on v2 (email index v2 §5; slice 8): one slots call, then the real
EmailCardSearchService over the in-memory card index, with the live headers and the per-key lists of v1. A test-local
scripted LLMPort answers the slots call; the prompt builder serves the real snapshot token. Invented cast only:
Alex Doe in Europe/Lisbon, Today 2026-11-16 (Monday)."""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

import pytest

from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.email_search_agent import EmailSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.confirmed_people import ConfirmedPeople
from src.domain.email import EmailMetadata
from src.domain.email_query_slots import SLOTS_PROFILE
from src.domain.email_search_render import SECTION_MAIL, SECTION_TOOL
from src.domain.user import PerformanceTier
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.llm_port import AgentExecutionContext, LLMPort, LLMRequest, LLMResponse, ProviderCapabilities
from src.services.email_card_search_service import EmailCardSearchService
from tests.support import prompt_render as pr
from tests.unit.domain.email_card_fixtures import PEOPLE
from tests.unit.domain.email_search_fixtures import everything

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
STAMP = "[Mon 16 Nov 2026, 10:00 UTC] "
USER, ACCOUNT = "usr-alex-0001", "acc-1"
FLAT = [1.0, 0.0, 0.0]
NO_SLOTS = {"subject": "", "doc_types": [], "domains": [], "people": [], "time": {"intent": "none", "from": "", "to": ""},
            "exact_keys": [], "scope": "top"}
SLOTS_PROMPT = pr.render_static(SLOTS_PROFILE)     # the snapshot token, assembled (placeholders still in)


class ScriptedSlotsLLM(LLMPort):
    """Answers the slots call from a script (text, or an exception to raise); records every request."""

    def __init__(self, script: Sequence[Union[str, BaseException]]) -> None:
        self.script = list(script)
        self.requests: List[LLMRequest] = []

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(text=item)

    async def upload_file(self, path: str, mime_type: str) -> Any:
        raise NotImplementedError

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return "gemini-eco"

    def supports_caching(self) -> bool:
        return False


class SnapshotPrompts:
    """Serves the snapshot's assembled static prompt per agent type; records the calls; `fail` raises."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    async def build_for_agent(self, agent_type: str, **kwargs: Any) -> str:
        self.calls.append({"agent_type": agent_type, **kwargs})
        if self.fail:
            raise RuntimeError("token missing")
        return SLOTS_PROMPT if agent_type == SLOTS_PROFILE else "v1 prompt"


class Embeddings(EmbeddingService):
    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        return FLAT

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [FLAT for _ in texts]


class People(ConfirmedPeopleRepository):
    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        return PEOPLE

    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        raise AssertionError("search never writes the list")


class BrokenIndex(InMemoryEmailCardIndex):
    async def count_uncarded(self, user_id: str) -> int:
        raise RuntimeError("index unavailable")


class Mailbox:
    """The v1 service's live side: the watermark and the header listings (by key; None = the fresh one)."""

    def __init__(self, watermark: Optional[datetime] = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)) -> None:
        self.watermark = watermark
        self.listing: Dict[Optional[str], Any] = {}
        self.calls: List[dict] = []
        self.vector_calls: List[dict] = []

    async def vector_search(self, **kwargs: Any) -> str:
        self.vector_calls.append(kwargs)
        return "No emails found matching your query."

    async def index_watermark(self, user_id: str) -> Optional[datetime]:
        return self.watermark

    async def list_headers(self, user_id, *, after=None, before=None, key=None, limit=100):
        self.calls.append({"after": after, "before": before, "key": key, "limit": limit})
        return self.listing.get(key, ([], False))

    def fresh_calls(self) -> List[dict]:
        return [c for c in self.calls if c["key"] is None]


def _meta(email_id: str, subject: str) -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject=subject, from_address="shop@tornillo.example",
                         date=datetime(2026, 9, 15, 9, 0), labels=[], snippet="first line")


def _index(cls=InMemoryEmailCardIndex) -> InMemoryEmailCardIndex:
    index = cls()
    for card in everything():
        index.add(USER, card, FLAT)
    return index


def _agent(llm: ScriptedSlotsLLM, *, mailbox: Optional[Mailbox] = None, prompts: Optional[SnapshotPrompts] = None,
           index: Optional[InMemoryEmailCardIndex] = None, v2: bool = True, tz: str = "Europe/Lisbon",
           now: datetime = NOW) -> EmailSearchAgent:
    context = AgentExecutionContext(agent_type="email_search", provider=llm, model_name="gemini-eco",
                                    tier=PerformanceTier.ECO, capabilities=ProviderCapabilities(),
                                    resilience_port=InMemoryProviderResilience())
    search = EmailCardSearchService(index if index is not None else _index(), Embeddings(), People()) if v2 else None
    return EmailSearchAgent(
        config=AgentConfig(agent_id="email_search_agent_usr", agent_type="email_search"), execution_context=context,
        prompt_builder=prompts or SnapshotPrompts(),  # type: ignore[arg-type]
        email_search_service=mailbox or Mailbox(),  # type: ignore[arg-type]
        user_id=USER, user_timezone=tz, clock=lambda: now, card_search=search,
    )


async def _run(agent: EmailSearchAgent, query: str) -> AgentResponse:
    return await agent.execute(AgentMessage(
        intent=AgentIntent.QUERY, payload={"query": query}, sender="smart", recipient="email_search", task_id="t1",
        context={"user_id": USER, "account_id": ACCOUNT}))


def _slots(**fields: Any) -> str:
    return json.dumps({**NO_SLOTS, **fields})


def _user_text(request: LLMRequest) -> str:
    return "".join(p.text or "" for p in request.messages[-1].parts)


async def test_a_stamped_query_is_searched_without_its_stamp_not_as_todays_list():
    llm = ScriptedSlotsLLM([_slots()])
    response = await _run(_agent(llm), STAMP + "what did Example School send")
    payload = json.loads(response.result)
    assert response.status == AgentStatus.SUCCESS
    assert _user_text(llm.requests[0]).startswith('EMAIL_SEARCH_REQUEST "what did Example School send"\n')
    assert payload["index"]["mode"] == "ranked"
    assert "msg_school_meeting" in payload["index"]["shown_ids"]
    assert "16 Nov 2026" not in "\n".join(text for _, text in payload["index"]["sections"])
    assert response.history_context["email_search_context"]["you_searched"] == "what did Example School send"


async def test_the_slots_message_carries_today_in_the_users_zone():
    llm = ScriptedSlotsLLM([_slots()])
    await _run(_agent(llm, tz="Asia/Tokyo", now=datetime(2026, 11, 16, 23, 30, tzinfo=UTC)), "school mail")
    assert _user_text(llm.requests[0]).endswith("\nToday: 2026-11-17 (Tuesday), timezone Asia/Tokyo.")


async def test_the_slots_call_is_the_json_call_of_the_rendered_profile():
    llm, prompts = ScriptedSlotsLLM([_slots()]), SnapshotPrompts()
    await _run(_agent(llm, prompts=prompts), "school mail")
    (call,) = prompts.calls
    assert call["agent_type"] == SLOTS_PROFILE
    assert not call["include_biographical"] and not call["include_directives"] and not call["include_location"]
    request = llm.requests[0]
    assert "{DOC_TYPES}" not in request.system_instruction and "insurance_policy" in request.system_instruction
    assert request.response_mime_type == "application/json" and request.response_schema["required"][0] == "subject"
    assert request.tools == [] and request.max_tokens == 1024


async def test_an_exact_key_hits_the_index_and_the_mailbox_and_the_shown_mail_is_not_listed_twice():
    mailbox = Mailbox()
    mailbox.listing["FA-0137"] = ([_meta("msg_invoice01", "Factura FA-0137"),
                                   _meta("msg_invoice_copy", "Fwd: Factura FA-0137")], False)
    llm = ScriptedSlotsLLM([_slots(subject="Invoice FA-0137 from the hardware shop", exact_keys=["FA-0137"])])
    payload = json.loads((await _run(_agent(llm, mailbox=mailbox), "the invoice FA-0137")).result)
    assert payload["index"]["shown_ids"][0] == "msg_invoice01"
    (group,) = payload["by_key"]
    assert group["key"] == "FA-0137" and group["listed"] == 2
    assert [e["email_id"] for e in group["emails"]] == ["msg_invoice_copy"]


async def test_a_folded_mail_is_not_listed_again_by_its_key():
    mailbox = Mailbox()
    mailbox.listing["0931"] = ([_meta("msg_receipt_2025-05-20", "Recibo de compra 20/05/2025")], False)
    llm = ScriptedSlotsLLM([_slots(exact_keys=["0931"], scope="all")])
    payload = json.loads((await _run(_agent(llm, mailbox=mailbox), "Supermercado Exemplo receipt No. 0931")).result)
    shown_or_folded = {*payload["index"]["shown_ids"], *(i for ids in payload["index"]["folded_ids"].values()
                                                         for i in ids)}
    assert "msg_receipt_2025-05-20" in shown_or_folded
    assert payload["by_key"][0]["emails"] == []


async def test_invalid_output_twice_degrades_to_no_slots_and_the_search_still_runs():
    llm = ScriptedSlotsLLM(["not json at all", "[1, 2]"])
    response = await _run(_agent(llm), "Example School parents meeting")
    assert response.status == AgentStatus.SUCCESS and response.metadata["slots"] == "degraded"
    assert len(llm.requests) == 2
    retry = _user_text(llm.requests[1])
    assert retry.startswith("Your previous output was not valid query slots (not JSON")
    assert "msg_school_meeting" in json.loads(response.result)["index"]["shown_ids"]


async def test_a_second_answer_after_a_bad_one_is_used():
    llm = ScriptedSlotsLLM(['"just a string"', _slots(doc_types=["insurance_policy"])])
    response = await _run(_agent(llm), "my insurance")
    assert response.metadata["slots"] == "ok" and len(llm.requests) == 2
    assert "not a JSON object" in _user_text(llm.requests[1])


async def test_a_failed_call_is_not_retried_and_degrades():
    llm = ScriptedSlotsLLM([RuntimeError("provider down")])
    response = await _run(_agent(llm), "Example School parents meeting")
    assert response.status == AgentStatus.SUCCESS and response.metadata["slots"] == "degraded"
    assert len(llm.requests) == 1


async def test_a_failed_prompt_build_makes_no_model_call():
    llm = ScriptedSlotsLLM([])
    response = await _run(_agent(llm, prompts=SnapshotPrompts(fail=True)), "Example School parents meeting")
    assert response.status == AgentStatus.SUCCESS and response.metadata["slots"] == "degraded"
    assert llm.requests == []


async def test_a_card_search_error_fails_the_search():
    llm = ScriptedSlotsLLM([_slots()])
    response = await _run(_agent(llm, index=_index(BrokenIndex)), "Example School parents meeting")
    assert response.status == AgentStatus.FAILED
    assert response.error.startswith("Email search failed: index unavailable")


async def test_a_named_month_bounds_the_live_listing_in_the_users_zone():
    mailbox = Mailbox(watermark=datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    await _run(_agent(ScriptedSlotsLLM([_slots()]), mailbox=mailbox), "receipts from September 2026")
    (fresh,) = mailbox.fresh_calls()
    # Lisbon is UTC+1 in September: the month starts at 23:00 UTC the day before
    assert fresh["after"] == datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
    assert fresh["before"] == datetime(2026, 9, 30, 22, 59, 59, 999999, tzinfo=UTC)


async def test_a_period_inside_the_index_skips_the_live_listing():
    mailbox = Mailbox(watermark=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    await _run(_agent(ScriptedSlotsLLM([_slots()]), mailbox=mailbox), "receipts from September 2026")
    assert mailbox.fresh_calls() == []


async def test_without_a_time_the_live_listing_opens_at_the_watermark():
    mailbox = Mailbox(watermark=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    await _run(_agent(ScriptedSlotsLLM([_slots()]), mailbox=mailbox), "Example School")
    assert mailbox.fresh_calls() == [{"after": datetime(2026, 11, 1, 6, 0, tzinfo=UTC), "before": None, "key": None,
                                      "limit": 100}]


async def test_an_upcoming_slot_does_not_bound_the_live_listing():
    mailbox = Mailbox(watermark=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    llm = ScriptedSlotsLLM([_slots(time={"intent": "upcoming", "from": "2026-11-16", "to": ""})])
    await _run(_agent(llm, mailbox=mailbox), "what insurance runs out soon")
    assert mailbox.fresh_calls()[0]["before"] is None


async def test_the_json_carries_the_index_block_and_never_the_v1_keys():
    mailbox = Mailbox()
    mailbox.listing[None] = ([_meta("msg_fresh01", "New mail")], False)
    response = await _run(_agent(ScriptedSlotsLLM([_slots()]), mailbox=mailbox), "Example School parents meeting")
    payload = json.loads(response.result)
    assert set(payload) == {"index", "fresh", "by_key", "notes"}
    index = payload["index"]
    assert set(index) == {"mode", "sections", "shown_ids", "folded_ids", "has_attachments"}
    assert all(kind in (SECTION_TOOL, SECTION_MAIL) and isinstance(text, str) for kind, text in index["sections"])
    assert index["sections"][0][0] == SECTION_TOOL
    assert [e["email_id"] for e in payload["fresh"]["emails"]] == ["msg_fresh01"]
    assert response.metadata["mode"] == index["mode"] and response.metadata["shown"] == len(index["shown_ids"])


async def test_nothing_found_is_still_the_json_never_the_v1_no_emails_line():
    index = InMemoryEmailCardIndex()
    response = await _run(_agent(ScriptedSlotsLLM([_slots()]), index=index), "Example Air boarding pass")
    payload = json.loads(response.result)
    assert payload["index"]["mode"] == "nothing" and payload["index"]["shown_ids"] == []
    assert response.history_context is None


async def test_you_received_lists_the_shown_cards_in_the_v1_shape():
    llm = ScriptedSlotsLLM([_slots(subject="Hotel booking in Rome")])
    response = await _run(_agent(llm), "my Rome hotel booking RM-55120")
    received = response.history_context["email_search_context"]["you_received"]
    rome = next(e for e in received if e["id"] == "msg_rome01")
    card = next(c for c in everything() if c.mail.email_id == "msg_rome01")
    assert rome == {"id": "msg_rome01", "from": "Albergo Immaginario", "date": "2025-06-02",
                    "summary": (card.profile.title or card.mail.subject)[:200],
                    "attachments": [a.name for a in card.attachments]}
    assert json.loads(response.result)["index"]["has_attachments"] is True


async def test_without_a_card_search_the_v1_profile_and_search_run():
    mailbox, prompts = Mailbox(), SnapshotPrompts()
    llm = ScriptedSlotsLLM([json.dumps({"primary_query": "school", "alternative_query": "school", "tags": []})])
    response = await _run(_agent(llm, mailbox=mailbox, prompts=prompts, v2=False), "Example School")
    assert [c["agent_type"] for c in prompts.calls] == ["email_search"]
    assert len(mailbox.vector_calls) == 1
    assert response.status == AgentStatus.SUCCESS and "index" not in (response.result or "")


@pytest.mark.parametrize("query", ["", STAMP])
async def test_an_empty_request_fails(query):
    response = await _run(_agent(ScriptedSlotsLLM([])), query)
    assert response.status == AgentStatus.FAILED and response.error == "No query provided"
