"""Facts S3 · T7: the consolidation run hands the agent the account owner's timezone (plan §3.10).

The real queue and lease run on the evaluating Firestore fake. The account and user repositories are fakes with
semantics (they hold documents and record which ids were read); the coordinator is the LLM boundary and keeps the
message it was routed. The last tests route the service's own message to a real `ConsolidationAgent` over the
evaluating fact store, so the stored expiry proves the zone crossed both seams.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentConfig, AgentResponse
from src.domain.billing import BillingAccount
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.domain.llm import LLMResponse, ToolCall
from src.domain.user import PerformanceTier, UserBotConfig, UserProfile
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.consolidation_service import ConsolidationService
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, on
from tests.support.firestore_eval import EvalDb

ACCOUNT = "account-abcdef123456"
OWNER = "owner-user-1"
MEMBER = "member-user-2"
VIEWER = "viewer-user-3"
PARIS = "Europe/Paris"
TOKYO = "Asia/Tokyo"
UNRESOLVED = "owner timezone unresolved for account"


def profile(user_id: str, tz: str = "UTC", account_id: Optional[str] = ACCOUNT) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=account_id, config=UserBotConfig(timezone=tz))


def account(**roles: str) -> BillingAccount:
    return BillingAccount(account_id=ACCOUNT, iam_policy=dict(roles))


class Accounts:
    """The account store: holds accounts, records every id asked for, can be made to raise."""

    def __init__(self, *held: BillingAccount, raises: Optional[Exception] = None) -> None:
        self.held = {a.account_id: a for a in held}
        self.asked: List[str] = []
        self.raises = raises

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        self.asked.append(account_id)
        if self.raises:
            raise self.raises
        return self.held.get(account_id)


class Users:
    """The user store: holds profiles, records every id asked for, can be made to raise for chosen ids."""

    def __init__(self, *held: UserProfile, raises_for: Optional[Dict[str, Exception]] = None) -> None:
        self.held = {p.user_id: p for p in held}
        self.asked: List[str] = []
        self.raises_for = raises_for or {}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        self.asked.append(user_id)
        if user_id in self.raises_for:
            raise self.raises_for[user_id]
        return self.held.get(user_id)


class CachedAgents:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


class Coordinator:
    def __init__(self) -> None:
        self.messages: List[Any] = []

    async def route_message(self, message):
        self.messages.append(message)
        return AgentResponse.success(result={}, agent_id="c", task_id=message.payload["batch_id"])

    @property
    def zones(self) -> List[Any]:
        return [m.context.get("owner_timezone") for m in self.messages]


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def queue(db):
    return FirestoreConsolidationQueue(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))


@pytest.fixture
def coordinator():
    return Coordinator()


def build(queue, db, coordinator, users: Users, accounts: Accounts) -> ConsolidationService:
    return ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=CachedAgents(), lease=FirestoreMemoryWriteLease(db, EnvironmentConfig()),
        run_timeout_s=900.0, user_repo=users, account_repo=accounts,
    )


async def put(queue, batch_id: str, user_id: str, created_at: float = 1.0, messages: Optional[list] = None) -> None:
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=user_id, session_id="s", messages=messages or [{"role": "user"}],
        status=BatchStatus.PENDING, created_at=created_at,
    ))


def warnings(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def unresolved(caplog) -> List[str]:
    return [w for w in warnings(caplog) if UNRESOLVED in w]


# ------------------------------------------------------------------------------------------------ the zone is resolved


async def test_the_owners_zone_reaches_the_message(queue, db, coordinator, caplog):
    users = Users(profile(OWNER, PARIS))
    service = build(queue, db, coordinator, users, Accounts(account(**{OWNER: "owner"})))
    await put(queue, "b1", OWNER)

    with caplog.at_level(logging.WARNING):
        await service.process_user_batches(OWNER)

    assert coordinator.zones == [PARIS]
    assert warnings(caplog) == []


async def test_a_members_batch_gets_the_owners_zone_not_the_members(queue, db, coordinator):
    users = Users(profile(OWNER, PARIS), profile(MEMBER, TOKYO))
    accounts = Accounts(account(**{OWNER: "owner", MEMBER: "member", VIEWER: "viewer"}))
    service = build(queue, db, coordinator, users, accounts)
    await put(queue, "b1", MEMBER)

    await service.process_user_batches(MEMBER)

    assert coordinator.zones == [PARIS]
    assert MEMBER in users.asked and OWNER in users.asked


async def test_the_zone_is_resolved_once_per_run_not_per_batch(queue, db, coordinator):
    users = Users(profile(OWNER, PARIS))
    accounts = Accounts(account(**{OWNER: "owner"}))
    service = build(queue, db, coordinator, users, accounts)
    await put(queue, "b1", OWNER, created_at=1.0)
    await put(queue, "b2", OWNER, created_at=2.0)
    await put(queue, "b3", OWNER, created_at=3.0)

    await service.process_user_batches(OWNER)

    assert coordinator.zones == [PARIS, PARIS, PARIS]
    assert accounts.asked == [ACCOUNT]
    assert users.asked.count(OWNER) == 2          # the batch user's own profile read, and the resolution: both the owner here


async def test_several_owners_use_the_first_in_sorted_order_and_warn_once(queue, db, coordinator, caplog):
    first, second = "aaa-owner", "zzz-owner"
    users = Users(profile(OWNER, PARIS, ), profile(first, TOKYO), profile(second, "America/New_York"))
    accounts = Accounts(account(**{second: "owner", first: "owner", OWNER: "member"}))
    service = build(queue, db, coordinator, users, accounts)
    await put(queue, "b1", OWNER)

    with caplog.at_level(logging.WARNING):
        await service.process_user_batches(OWNER)

    assert coordinator.zones == [TOKYO]
    several = [w for w in warnings(caplog) if "2 owners" in w]
    assert len(several) == 1 and ACCOUNT[:8] in several[0]
    assert unresolved(caplog) == []


# ------------------------------------------------------------------------------------------------ the fallback


def _case(name, accounts, users):
    return pytest.param(accounts, users, id=name)


@pytest.mark.parametrize("accounts,users", [
    _case("no-account", Accounts(), Users(profile(OWNER, PARIS))),
    _case("no-owner-in-the-policy", Accounts(account(**{OWNER: "member"})), Users(profile(OWNER, PARIS))),
    _case("empty-policy", Accounts(account()), Users(profile(OWNER, PARIS))),
    _case("owner-without-a-profile", Accounts(account(**{OWNER: "owner", MEMBER: "member"})), Users(profile(MEMBER, TOKYO))),
    _case("unknown-zone", Accounts(account(**{OWNER: "owner"})), Users(profile(OWNER, "Mars/Olympus"))),
    _case("blank-zone", Accounts(account(**{OWNER: "owner"})), Users(profile(OWNER, ""))),
    _case("account-store-raises", Accounts(account(**{OWNER: "owner"}), raises=RuntimeError("firestore unavailable")),
          Users(profile(OWNER, PARIS))),
    _case("owner-read-raises", Accounts(account(**{OWNER: "owner", MEMBER: "member"})),
          Users(profile(MEMBER, TOKYO), raises_for={OWNER: TimeoutError("deadline")})),
])
async def test_anything_unresolved_gives_utc_one_warning_and_the_batch_still_runs(
    queue, db, coordinator, accounts, users, caplog,
):
    service = build(queue, db, coordinator, users, accounts)
    batch_user = MEMBER if MEMBER in users.held else OWNER
    users.held.setdefault(batch_user, profile(batch_user, "UTC"))
    await put(queue, "b1", batch_user)

    with caplog.at_level(logging.WARNING):
        await service.process_user_batches(batch_user)

    assert coordinator.zones == ["UTC"], "the batch is still routed, with UTC"
    assert (await queue.collection.document("b1").get()).exists is False, "and consolidated"
    found = unresolved(caplog)
    assert len(found) == 1 and ACCOUNT[:8] in found[0] and found[0].endswith("; using UTC")


async def test_a_raising_store_names_the_exception_in_the_warning(queue, db, coordinator, caplog):
    accounts = Accounts(raises=RuntimeError("firestore unavailable"))
    service = build(queue, db, coordinator, Users(profile(OWNER, PARIS)), accounts)
    await put(queue, "b1", OWNER)

    with caplog.at_level(logging.WARNING):
        await service.process_user_batches(OWNER)

    assert "RuntimeError: firestore unavailable" in unresolved(caplog)[0]


async def test_the_user_id_is_never_read_as_an_account(queue, db, coordinator):
    users = Users(profile(OWNER, PARIS))
    accounts = Accounts()                                    # the account is missing
    service = build(queue, db, coordinator, users, accounts)
    await put(queue, "b1", OWNER)

    await service.process_user_batches(OWNER)

    assert accounts.asked == [ACCOUNT]
    assert coordinator.zones == ["UTC"]


async def test_a_user_without_an_account_still_fails_the_batch_and_reads_no_account(queue, db, coordinator):
    users = Users(profile(OWNER, PARIS, account_id=""))
    accounts = Accounts(account(**{OWNER: "owner"}))
    service = build(queue, db, coordinator, users, accounts)
    await put(queue, "b1", OWNER)

    await service.process_user_batches(OWNER)

    assert accounts.asked == [] and coordinator.messages == []
    assert (await queue.collection.document("b1").get()).to_dict()["status"] == BatchStatus.FAILED.value


def test_the_account_repository_is_required(queue, db, coordinator):
    with pytest.raises(TypeError, match="account_repo"):
        ConsolidationService(
            queue=queue, coordinator=coordinator, agent_factory=CachedAgents(),
            lease=FirestoreMemoryWriteLease(db, EnvironmentConfig()), run_timeout_s=900.0, user_repo=Users(),
        )


# ------------------------------------------------------------------------------------------------ through the real agent


def _real_agent(store: FactStore):
    """A real ConsolidationAgent over the evaluating fact store; its LLM creates one dated fact in Stage 1, then reports."""
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
        fact_management_port=port, prompt_builder=pb, indexed_email_repo=None,
    )
    agent.INLINE_CLUSTER_REVIEW = False
    store.repo.refresh_biographical_context_cache = AsyncMock()
    spent: set = set()

    async def call(request, turn=1):
        first = request.messages[0].parts[0].text
        if "Begin deliberate consolidation." in first and "stage1" not in spent:
            spent.add("stage1")
            attributes = {"domain": "biographical", "temporal_class": "stable", "context_priority": "medium",
                          "tags": ["t"], "metadata": {}, "type": "event", "expiry": on("2027-01-15")}
            return LLMResponse(text=None, tool_calls=[
                ToolCall(name="create_fact", args={"content": "trip to Rome", "fact_attributes": attributes})])
        return LLMResponse(text='{"operations": []}')

    agent._call_llm = call
    return agent


class AgentCoordinator:
    """Routes the service's own message to the real agent, as the production coordinator does."""

    def __init__(self, agent: ConsolidationAgent) -> None:
        self.agent = agent

    async def route_message(self, message):
        return await self.agent.execute(message)


@pytest.mark.parametrize("owner_zone,expected_expiry", [
    (PARIS, datetime(2027, 1, 15, 23, tzinfo=timezone.utc)),         # the end of 15 Jan in Paris
    (TOKYO, datetime(2027, 1, 15, 15, tzinfo=timezone.utc)),         # the end of 15 Jan in Tokyo
])
async def test_the_production_path_writes_the_expiry_in_the_owners_zone_not_in_utc(queue, db, owner_zone, expected_expiry):
    from tests.support.fact_kit import ACC as FACT_ACC, USER as FACT_USER

    store = FactStore(indexes=INDEXES_FILE)
    users = Users(profile(FACT_USER, "UTC", account_id=FACT_ACC), profile(OWNER, owner_zone, account_id=FACT_ACC))
    accounts = Accounts(BillingAccount(account_id=FACT_ACC, iam_policy={OWNER: "owner", FACT_USER: "member"}))
    service = build(queue, db, AgentCoordinator(_real_agent(store)), users, accounts)
    await put(queue, "b1", FACT_USER, messages=[
        {"role": "user", "text": "I am going to Rome", "created_at": datetime(2026, 10, 20, 11, tzinfo=timezone.utc).timestamp()},
    ])

    await service.process_user_batches(FACT_USER)

    (fact,) = [d for d in store.docs.values() if d["text"] == "trip to Rome"]
    assert fact["expires_at"].astimezone(timezone.utc) == expected_expiry
