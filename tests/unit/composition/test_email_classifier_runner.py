"""EmailClassifierRunner: the email classifier of an indexing job runs with the job user's configuration, is billed to the
job's account and is recorded in prompt_content under the job's user and account (roadmap 14b, P9 finding D2).

The world: the real `EmailIndexingService` with the runner as its classifier, the real `AgentContextBuilder` over a
registry whose `gemini` is a scripted model, the real `FirestoreQuotaService` over `FirestoreAccountRepository` on the
evaluating Firestore fake (the account ledgers), and the real `BigQueryPromptContentAdapter` record building with only
its BigQuery write replaced (the recorded rows). Mail, the job repository and the other indexing stores are scripted
boundaries; nothing is valuable, so no page reaches the embedder.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.adapters.bigquery_prompt_content_adapter import BigQueryPromptContentAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_quota_service import FirestoreQuotaService
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.connector_registry import ConnectorRegistry
from src.composition.email_classifier_runner import EmailClassifierRunner
from src.domain.billing import BillingAccount
from src.domain.email import EmailMetadata, IndexingJob
from src.domain.integration import Integration
from src.domain.llm import LLMRequest, LLMResponse, ProviderCapabilities, UsageMetadata
from src.domain.oauth_credential import OAuthCredential
from src.domain.observability import PromptContentRecord
from src.domain.provider_account import ProviderAccount
from src.domain.user import PerformanceTier, UserBotConfig, UserProfile
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.llm_port import LLMPort
from src.services.agent_context_builder import AgentContextBuilder
from src.services.email_indexing_service import EmailIndexingService
from src.services.provider_registry import ProviderRegistry
from src.utils.telemetry import get_request_context, request_user_scope
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
ACCOUNTS = "eval_domain_accounts_v2"

# The Gemini models of the tiers the tests use (GeminiAdapter.MODEL_TIERS), so the ledger prices real models.
MODELS = {PerformanceTier.ECO: "gemini-3.5-flash-lite", PerformanceTier.BALANCED: "gemini-3.8-flash"}
# What the scripted model reports per account (told apart by the account-specific prompt), so a ledger that got the
# other account's call shows a different count.
TOKENS = {"acc-1": (1200, 80), "acc-2": (3400, 150)}


class _Model(LLMPort):
    """The `gemini` provider: answers every request with an empty classification (nothing valuable) and the usage of
    the request's account; records each request. With `rendezvous=n`, a request waits until n requests are in flight
    (forces two concurrent batches to overlap) and `peak` shows that they did."""

    def __init__(self, rendezvous: int = 0) -> None:
        self.requests: List[LLMRequest] = []
        self.users: List[Optional[str]] = []   # the request context's user at each call
        self._rendezvous = rendezvous
        self._in_flight = 0
        self.peak = 0
        self._all_in = asyncio.Event()

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))
        self.users.append(get_request_context()["user_id"])
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        try:
            if self._rendezvous:
                if self._in_flight >= self._rendezvous:
                    self._all_in.set()
                await asyncio.wait_for(self._all_in.wait(), timeout=2)
            account = (request.system_instruction or "").split("prompt for ", 1)[1].split("\n", 1)[0]
            prompt_tokens, completion_tokens = TOKENS[account]
            return LLMResponse(text=json.dumps([]), usage_metadata=UsageMetadata(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens))
        finally:
            self._in_flight -= 1

    async def upload_file(self, path: str, mime_type: str):  # pragma: no cover - never called
        raise NotImplementedError

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return MODELS[tier]

    def supports_caching(self) -> bool:
        return False


class _Prompts:
    """The prompt store: a prompt that names the account it was assembled for."""

    async def build_for_agent(self, *, agent_type: str, user_id: str, account_id: Optional[str],
                              include_biographical: bool, **_: object) -> str:
        return f"prompt for {account_id}\n"


class _Users:
    def __init__(self, *profiles: UserProfile) -> None:
        self._by_id: Dict[str, UserProfile] = {p.user_id: p for p in profiles}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self._by_id.get(user_id)


class _Rows(BigQueryPromptContentAdapter):
    """The real record building; the BigQuery write is replaced by a list."""

    def __init__(self) -> None:
        super().__init__(dataset="observability", table="prompt_content")
        self.rows: List[PromptContentRecord] = []

    async def _store(self, record: PromptContentRecord) -> None:
        self.rows.append(record)


class World:
    def __init__(self, model: _Model, *profiles: UserProfile) -> None:
        self.model = model
        self.db = EvalDb(indexes=INDEXES_FILE, groups={ACCOUNTS: "development_domain_accounts_v2"})
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.store = _Rows()
        registry = ProviderRegistry()
        registry.register("gemini", model)
        self.runner = EmailClassifierRunner(
            context_builder=AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience()),
            user_repo=_Users(*profiles), prompt_builder=_Prompts(), gmail=None,
            quota_service=FirestoreQuotaService(self.accounts), prompt_content_store=self.store,
        )

    async def seed(self) -> None:
        for account_id in ("acc-1", "acc-2"):
            await self.accounts.create_account(BillingAccount(account_id=account_id))

    def usage(self, account_id: str) -> dict:
        return dict(self.db.collection(ACCOUNTS).docs[account_id].get("usage") or {})

    def service(self) -> EmailIndexingService:
        gmail = AsyncMock(spec=EmailProviderPort)
        gmail.list_emails.return_value = ([_meta("m1"), _meta("m2")], None)
        repo = AsyncMock(spec=IndexedEmailRepository)
        repo.get_indexing_state.return_value = None
        exclusions = AsyncMock(spec=EmailExclusionsPort)
        exclusions.get_exclusions.return_value = []
        return EmailIndexingService(
            gmail=gmail, email_repo=repo, job_repo=AsyncMock(spec=EmailIndexingJobRepository),
            exclusions_repo=exclusions, classifier=self.runner, embedding=AsyncMock(spec=EmbeddingService),
            oauth=None, connector=ConnectorRegistry().get("gmail"),
        )

    async def index(self, user_id: str, account_id: str) -> IndexingJob:
        job = _job(user_id, account_id)
        return await self.service().run_indexing_job(job, _credentials(user_id, account_id), account_id)


def _profile(user_id: str, account_id: str, **config: object) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=account_id, config=UserBotConfig(**config))


def _meta(email_id: str) -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject="Gym schedule", from_address="robin@example.com",
                         date=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc), labels=["INBOX"],
                         snippet="The gym opens at seven")


def _job(user_id: str, account_id: str) -> IndexingJob:
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    return IndexingJob(job_id=f"job-{user_id}", user_id=user_id, account_id=account_id, provider="gmail",
                       triggered_by="test", status="running", started_at=now, updated_at=now)


def _credentials(user_id: str, account_id: str) -> OAuthCredential:
    return OAuthCredential(
        user_id=user_id, account_id=account_id, kind="gmail", integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="robin@example.com"),
        access_token="tok", refresh_token="rtok", token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
        scopes=["gmail.readonly"],
    )


def _tokens(account_id: str) -> int:
    return sum(TOKENS[account_id])


@pytest.fixture
async def world() -> World:
    w = World(_Model(), _profile("usr-1", "acc-1"), _profile("usr-2", "acc-2"))
    await w.seed()
    return w


async def test_a_classify_call_during_an_indexing_job_is_billed_to_the_jobs_account(world):
    job = await world.index("usr-1", "acc-1")

    assert job.status == "completed" and job.emails_failed == 0
    assert len(world.model.requests) == 1
    usage = world.usage("acc-1")
    assert usage["daily_tokens"] == _tokens("acc-1")
    assert usage["total_requests"] == 1
    assert usage["daily_cost"] > 0
    assert world.usage("acc-2").get("daily_tokens", 0) == 0


async def test_the_recorded_prompt_content_row_carries_the_jobs_user_and_account(world):
    await world.index("usr-1", "acc-1")
    await world.store.flush()

    assert [(r.user_id, r.account_id, r.agent_type) for r in world.store.rows] == [
        ("usr-1", "acc-1", "email_classifier")]
    assert world.store.rows[0].total_tokens == _tokens("acc-1")
    assert world.store.rows[0].agent_id == "email_classifier_usr-1"


async def test_the_prompt_is_assembled_for_the_jobs_account(world):
    await world.index("usr-1", "acc-1")

    assert world.model.requests[0].system_instruction.startswith("prompt for acc-1\n")


async def test_a_users_tier_override_for_the_classifier_reaches_its_execution_context():
    world = World(_Model(), _profile("usr-1", "acc-1", agent_tiers={"email_classifier": PerformanceTier.ECO}),
                  _profile("usr-2", "acc-2"))
    await world.seed()

    await world.index("usr-1", "acc-1")
    await world.index("usr-2", "acc-2")

    assert [r.model_name for r in world.model.requests] == [MODELS[PerformanceTier.ECO],
                                                            MODELS[PerformanceTier.BALANCED]]
    await world.store.flush()
    assert [(r.user_id, r.model) for r in world.store.rows] == [("usr-1", MODELS[PerformanceTier.ECO]),
                                                               ("usr-2", MODELS[PerformanceTier.BALANCED])]


async def test_a_users_model_override_for_the_classifier_reaches_its_execution_context():
    world = World(_Model(), _profile("usr-1", "acc-1", model_overrides={"email_classifier": "gemini-pro-latest"}))
    await world.seed()

    await world.index("usr-1", "acc-1")

    assert [r.model_name for r in world.model.requests] == ["gemini-pro-latest"]


async def test_two_accounts_indexed_concurrently_bill_and_record_each_to_its_own():
    world = World(_Model(rendezvous=2), _profile("usr-1", "acc-1"), _profile("usr-2", "acc-2"))
    await world.seed()

    jobs = await asyncio.gather(world.index("usr-1", "acc-1"), world.index("usr-2", "acc-2"))

    assert world.model.peak == 2, "both batches were at the model at once"
    assert [j.status for j in jobs] == ["completed", "completed"]
    assert world.usage("acc-1")["daily_tokens"] == _tokens("acc-1")
    assert world.usage("acc-2")["daily_tokens"] == _tokens("acc-2")
    await world.store.flush()
    assert sorted((r.user_id, r.account_id, r.total_tokens) for r in world.store.rows) == [
        ("usr-1", "acc-1", _tokens("acc-1")), ("usr-2", "acc-2", _tokens("acc-2"))]


async def test_the_batch_runs_as_the_jobs_user_and_restores_the_request_user(world):
    with request_user_scope("usr-9"):
        await world.runner.classify_batch([_meta("m1")], "usr-1", account_id="acc-1")
        after = get_request_context()["user_id"]

    assert world.model.users == ["usr-1"]
    assert after == "usr-9"


async def test_an_empty_batch_builds_nothing_and_bills_nothing(world):
    assert await world.runner.classify_batch([], "usr-unknown", account_id="acc-1") == []
    assert world.model.requests == []
    assert world.usage("acc-1").get("daily_tokens", 0) == 0


async def test_an_unknown_user_fails_the_job_and_bills_nothing(world):
    with pytest.raises(RuntimeError, match="not found"):
        await world.index("usr-3", "acc-1")

    assert world.model.requests == []
    assert world.usage("acc-1").get("daily_tokens", 0) == 0


async def test_a_classifier_that_cannot_be_built_fails_the_batch(world):
    class _Broken:
        def build(self, agent_type: str, config: UserBotConfig) -> None:
            raise KeyError("gemini")

    world.runner._context_builder = _Broken()

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await world.runner.classify_batch([_meta("m1")], "usr-1", account_id="acc-1")
    assert world.model.requests == []


async def test_a_runner_without_a_user_repository_refuses():
    runner = EmailClassifierRunner(context_builder=None, user_repo=None, prompt_builder=_Prompts())

    with pytest.raises(RuntimeError, match="no user repository"):
        await runner.classify_batch([_meta("m1")], "usr-1", account_id="acc-1")
