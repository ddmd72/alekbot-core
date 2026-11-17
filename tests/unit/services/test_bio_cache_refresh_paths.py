"""Every production path that changes which facts an account's prompt sees leaves the account's biographical cache rebuilt
after its write: the safety net of the repository / BiographicalContextService untangling (C3 · S9).

Outcome seam: what `get_biographical_context_cached` (the read of PromptBuilder, the router, Smart and Quick) returns for the
account after the path ran, never a refresh call. Each test seeds its facts without refreshing, then plants a cached copy
that no fact holds (`PLANTED`) in the account's cache and in another account's, runs the path and reads both: the account
reads its current facts, the other account still reads its planted copy (the refresh went to the account id, not the user
id or another account).

Limits are pinned through the content: the account's own defaults (`ACCOUNT_LIMITS`, read by the context service through
the account store as in production) hold where a caller passes no limit; the consolidator's explicit limits
(`AGENT_LIMITS`) win over them. The background (`seed_background`) is sized so the two give different caches.

The repository and the context service are wired as `ServiceContainer` wires them (`account_fact_world`); the review and
janitor worlds are built on it by replacing their `make_fact_world`. The failure contract (port docstring: a refresh never
raises into the write path, the previous cache stays) fails the refresh at the two boundaries every design crosses: the
read of the account's facts and the cache document write."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.billing import BillingAccount
from src.domain.email_memory_candidate import EMAIL_CANDIDATES_HEADER
from src.domain.entities import ContextPriority, ExpiryKind, FactDomain
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_profile_run_limits import FactProfileRunLimits
from src.domain.fact_review_task import FactReviewTask, ReviewItem, encode_review_task
from src.domain.llm import LLMResponse, ToolCall
from src.domain.memory_write_lease import LEASE_MARGIN
from src.domain.place_names import PlaceNames
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.agent_manifest import Intent
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.biographical_context_service import BiographicalContextService
from src.services.cabinet_fact_service import CabinetFactService
from src.services.configuration_service import ConfigurationService
from src.services.fact_profile_service import FactProfileService
from src.services.fact_write_service import FactWriteService
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support import fact_lifecycle_world, fact_review_world
from tests.support.fact_kit import ACC, OTHER, USER, make_fact
from tests.support.fact_lifecycle_world import NOW, FaultyRepo, JanitorWorld
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config
from tests.support.firestore_eval import EvalDocRef
from tests.support.stored_mail import mail_store_with, stored_mail
from tests.unit.services.test_fact_answer_service import question_on, service as answer_service
from tests.unit.services.test_fact_profile_service import ATLAS_DIGEST, BUDGET, ROME, Clock, Embedder, Profiler
from tests.unit.services.test_fact_review_selection import Rig as ReviewRig

CACHE = make_env_config().user_context_collection
PLANTED = "a cached copy no fact holds"

# The account's own cache limits, and the consolidator's explicit ones (production passes the user's resolved limits).
ACCOUNT_LIMITS = UserBotConfig(biographical_cache_limit=3, principles_cache_limit=1)
AGENT_LIMITS = {"facts_limit": 5, "principles_limit": 2}

# The background: two principles (mindset, medium) above three low-priority facts, oldest first in each group.
VEGETARIAN = "Alex is vegetarian"
YOGA = "Alex does morning yoga for 30 minutes"
ACME = "the user works at Acme"
MARIA = "the user's mother is Maria"
SKODA = "Alex owns the 2015 Skoda Fabia"
BACKGROUND = (
    (VEGETARIAN, ContextPriority.MEDIUM, ["mindset"], 2),
    (YOGA, ContextPriority.MEDIUM, ["mindset"], 1),
    (ACME, ContextPriority.LOW, [], 3),
    (MARIA, ContextPriority.LOW, [], 2),
    (SKODA, ContextPriority.LOW, [], 1),
)
# What the account limits keep (three facts, one principle) when the subject fact is not current: the subject is high
# priority, so when it is current it takes the one fact place.
SUBJECT_GONE = ([ACME], [VEGETARIAN])

LIVES = "Alex lives in Springfield"
CHESS = "plays chess on Sundays"
TENNIS = "plays tennis on Sundays"


class AccountLimits:
    """The account store as the context service reads it: ACC carries `ACCOUNT_LIMITS` as its account defaults."""

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        if account_id != ACC:
            return None
        return BillingAccount(account_id=ACC, account_defaults=ACCOUNT_LIMITS)


def agent_view_of_new(new: str) -> Tuple[List[str], List[str]]:
    """What the consolidator's limits keep (five facts, two principles) around a new medium fact, newer than the
    background; the account limits would keep `([new], [VEGETARIAN])`."""
    return [new, ACME, MARIA], [VEGETARIAN, YOGA]


def account_fact_world() -> Tuple[FactStore, FaultyRepo, BiographicalContextService]:
    """`make_fact_world` with the production wiring of the context service (`ServiceContainer`): the configuration
    service and an account store, so a refresh without explicit limits keeps the account's own."""
    store = FactStore(indexes=INDEXES_FILE)
    repo = FaultyRepo(store.db, make_env_config(), embedding_service=store.embedder)
    repo.fail_on = set()
    bio = BiographicalContextService(repo, config_service=ConfigurationService(), account_repo=AccountLimits())
    return store, repo, bio


@pytest.fixture
def worlds(monkeypatch):
    """The review and janitor worlds are built on `account_fact_world`."""
    monkeypatch.setattr(fact_review_world, "make_fact_world", account_fact_world)
    monkeypatch.setattr(fact_lifecycle_world, "make_fact_world", account_fact_world)


async def seed_background(repo: Any) -> None:
    for text, priority, tags, age in BACKGROUND:
        await repo.add_fact(make_fact(text, domain=FactDomain.BIOGRAPHICAL, context_priority=priority, tags=tags,
                                      age_days=age))


def subject(**over: Any) -> Dict[str, Any]:
    """The fields of a high-priority biographical fact: the one the path under test changes."""
    return dict(domain=FactDomain.BIOGRAPHICAL, context_priority=ContextPriority.HIGH, age_days=4, **over)


def plant(db: Any) -> None:
    """A cache that no fact holds, in the account's cache document and in another account's."""
    for account in (ACC, OTHER):
        db.collection(CACHE).docs[account] = {
            "biographical_facts": [{"id": "planted", "text": PLANTED, "domain": "biographical", "tags": [],
                                    "context_priority": "high", "created_at": None}],
            "principles": [], "directives": [],
        }


async def prompt_view(repo: Any, account: str = ACC) -> Tuple[List[str], List[str]]:
    """What the account's prompt reads: (fact texts, principle texts), in cache order."""
    items = await repo.get_biographical_context_cached(account)
    return ([i["text"] for i in items if "mindset" not in i["tags"]],
            [i["text"] for i in items if "mindset" in i["tags"]])


async def assert_rebuilt(repo: Any, facts: List[str], principles: List[str]) -> None:
    assert await prompt_view(repo) == (facts, principles)
    assert await prompt_view(repo, OTHER) == ([PLANTED], [])             # another account's cache is not touched


# ------------------------------------------------------------------------------------------------ the consolidator

REPORT = LLMResponse(text='{"operations": []}')
STAGE1 = "Begin deliberate consolidation."
CLUSTER = "FACT CLUSTER REVIEW"
ATTRS = {"domain": "biographical", "temporal_class": "stable", "context_priority": "medium", "tags": ["t"],
         "metadata": {}, "type": "event", "expiry": {"kind": "none"}}
HORIZON = {"kind": "horizon", "days": 30}


class Prompts:
    """The prompt builder as the consolidator uses it: one fixed prompt per profile."""

    async def build_for_agent(self, agent_type: str, **_: Any) -> str:
        return f"PROMPT {agent_type}"

    def invalidate_biographical_cache(self, account_id: str) -> None:
        pass


def create(content: str) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall(name="create_fact", args={"content": content, "fact_attributes": dict(ATTRS)})])


def consolidator(repo: Any, store: FactStore, bio: Any, *, email_repo: Any = None,
                 fact_updates: Any = None) -> ConsolidationAgent:
    """The agent over the world's repository, the real fact management and typed write path, with `AGENT_LIMITS`."""
    port = FactManagementAdapter(
        repo, store.embedder, FactWriteService(repo, store.embedder),
        SearchEnrichmentService(repo, store.embedder, 7, 10, 10, 20),
    )
    execution = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    return ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=execution,
        repository=repo, embedding_service=store.embedder, fact_write_service=FactWriteService(repo, store.embedder),
        fact_management_port=fact_updates or port, fact_updates=fact_updates or port, prompt_builder=Prompts(),
        indexed_email_repo=email_repo, email_memory_batches=email_repo, **AGENT_LIMITS, bio_cache=bio,
    )


def script(agent: ConsolidationAgent, first: Callable[[], LLMResponse], marker: Optional[str] = None,
           then: Optional[Callable[[], LLMResponse]] = None) -> None:
    """The model answers `first()` on the first turn of the pass whose first message holds `marker` (any pass: None),
    `then()` on that pass's later turns (default: the report), and reports on every other pass."""
    async def call(request: Any, turn: int = 1) -> LLMResponse:
        text = request.messages[0].parts[0].text or ""
        if marker is None or marker in text:
            if len(request.messages) == 1:
                return first()
            if then is not None:
                return then()
        return REPORT

    agent._call_llm = call  # type: ignore[method-assign]


def message(task: str, **payload: Any) -> AgentMessage:
    return AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                        payload={"task": task, **payload}, context={"user_id": USER, "owner_timezone": "UTC"})


async def execute(agent: ConsolidationAgent, task: str, **payload: Any) -> AgentResponse:
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent.execute(message(task, **payload))


BATCH = [{"role": "user", "text": "I play tennis on Sundays now", "created_at": "2026-10-22T10:00:00Z"}]


class TestConsolidationAgent:
    async def test_a_conversation_pass_that_writes_a_fact_rebuilds_the_cache_with_the_agents_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        plant(store.db)
        agent = consolidator(repo, store, bio)
        script(agent, lambda: create(TENNIS), STAGE1)

        response = await execute(agent, "consolidate", messages=BATCH)

        assert response.status is AgentStatus.SUCCESS, response
        await assert_rebuilt(repo, *agent_view_of_new(TENNIS))

    async def test_a_conversation_pass_that_fails_after_a_write_still_rebuilds_the_cache(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        plant(store.db)
        agent = consolidator(repo, store, bio)

        def provider_down() -> LLMResponse:
            raise RuntimeError("provider down")

        script(agent, lambda: create(TENNIS), STAGE1, then=provider_down)

        response = await execute(agent, "consolidate", messages=BATCH)

        assert response.status is AgentStatus.FAILED, response
        await assert_rebuilt(repo, *agent_view_of_new(TENNIS))

    async def test_a_legacy_v2_pass_that_writes_a_fact_rebuilds_the_cache_with_the_agents_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        plant(store.db)
        agent = consolidator(repo, store, bio)
        agent.prompt_version = "v2"
        fact = {"id": "n1", "content": TENNIS, "tags": ["t"], "domain": "biographical", "context_priority": "medium"}
        script(agent, lambda: LLMResponse(text='{"new_facts": [%s], "new_anchors": []}' % json.dumps(fact)))

        response = await execute(agent, "consolidate", messages=BATCH)

        assert response.status is AgentStatus.SUCCESS, response
        await assert_rebuilt(repo, *agent_view_of_new(TENNIS))

    async def test_a_cluster_review_rebuilds_the_cache_with_the_agents_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        plant(store.db)
        agent = consolidator(repo, store, bio)
        script(agent, lambda: create(TENNIS), CLUSTER)

        response = await execute(agent, "consolidate_cluster", cluster=[{"fact_id": "x", "content": "c", "similarity": 0.5}])

        assert response.status is AgentStatus.SUCCESS, response
        await assert_rebuilt(repo, *agent_view_of_new(TENNIS))

    async def test_an_email_pass_that_writes_a_fact_rebuilds_the_cache_with_the_agents_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        plant(store.db)
        mails = await mail_store_with(stored_mail("e1", user_id=USER, account_id=ACC,
                                                  sent=datetime(2026, 10, 10, 9, tzinfo=timezone.utc)))
        agent = consolidator(repo, store, bio, email_repo=mails)
        script(agent, lambda: create(TENNIS), EMAIL_CANDIDATES_HEADER)

        response = await execute(agent, "consolidate_email", number_of_batches=1)

        assert response.status is AgentStatus.SUCCESS and response.result["batches_processed"] == 1, response
        await assert_rebuilt(repo, *agent_view_of_new(TENNIS))

    async def test_a_review_pass_that_revives_a_stale_fact_rebuilds_the_cache_with_the_agents_limits(self, worlds):
        world = ReviewWorld()
        await seed_background(world.repo)
        stale = await world.seed_stale(CHESS, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                       reported_date=NOW - timedelta(days=60), **subject())
        evidence = await world.seed(TENNIS, reported_date=NOW - timedelta(days=1), context_priority=ContextPriority.ARCHIVAL)
        plant(world.store.db)
        agent = consolidator(world.repo, world.store, world.bio, fact_updates=world.store.adapter)
        agent._now = lambda: world.now  # type: ignore[method-assign]
        revive = {"fact_id": stale, "updates": {"state": "current", "expiry": HORIZON},
                  "basis": {"kind": "evidence", "evidence_fact_id": evidence}}
        script(agent, lambda: LLMResponse(text=None, tool_calls=[ToolCall(name="update_fact", args=revive)]))
        task = FactReviewTask(items=(ReviewItem(fact=await world.repo.get_fact_by_id(stale)),), mirror=False)

        response = await execute(agent, Intent.REVIEW_STALE_FACTS, **encode_review_task(task),
                                 biographical_context=[{"text": "bio fact"}])

        assert response.status is AgentStatus.SUCCESS, response
        # The revived fact is high priority: it comes first, the agent's five places take it, both principles and two facts.
        await assert_rebuilt(repo=world.repo, facts=[CHESS, ACME, MARIA], principles=[VEGETARIAN, YOGA])


# ------------------------------------------------------------------------------------------------ the services

class TestCabinetFactService:
    async def test_a_cabinet_delete_rebuilds_the_cache_with_the_accounts_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        await repo.add_fact(make_fact(LIVES, **subject()))
        plant(store.db)

        await CabinetFactService(repo, bio).invalidate(f"f-{LIVES}", ACC)

        await assert_rebuilt(repo, *SUBJECT_GONE)

    async def test_a_refresh_that_cannot_read_the_facts_never_fails_the_delete_and_the_cache_stays(self, monkeypatch):
        store, repo, bio = account_fact_world()
        await repo.add_fact(make_fact(LIVES, **subject()))
        plant(store.db)

        async def facts_unreadable(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("fact store unavailable")

        monkeypatch.setattr(repo, "get_active_facts_ordered", facts_unreadable)

        await CabinetFactService(repo, bio).invalidate(f"f-{LIVES}", ACC)

        assert store.docs[f"f-{LIVES}"]["state"] == "invalidated"
        assert await prompt_view(repo) == ([PLANTED], [])

    async def test_a_refresh_that_cannot_write_the_cache_never_fails_the_delete_and_the_cache_stays(self, monkeypatch):
        store, repo, bio = account_fact_world()
        await repo.add_fact(make_fact(LIVES, **subject()))
        plant(store.db)
        real_apply = EvalDocRef._apply

        def cache_unwritable(ref: EvalDocRef, *args: Any, **kwargs: Any) -> Any:
            if ref.col.name == CACHE:
                raise RuntimeError("cache store unavailable")
            return real_apply(ref, *args, **kwargs)

        monkeypatch.setattr(EvalDocRef, "_apply", cache_unwritable)

        await CabinetFactService(repo, bio).invalidate(f"f-{LIVES}", ACC)

        assert store.docs[f"f-{LIVES}"]["state"] == "invalidated"
        assert await prompt_view(repo) == ([PLANTED], [])


class TestFactAnswerService:
    async def test_an_owner_answer_that_revives_a_stale_fact_rebuilds_the_cache_with_the_accounts_limits(self, worlds):
        world = ReviewWorld()
        await seed_background(world.repo)
        fact = await world.seed_stale(CHESS, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                      reported_date=NOW - timedelta(days=60), **subject())
        await question_on(world, fact)
        plant(world.store.db)

        result = await answer_service(world).apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", world.now)

        assert result is AnswerResult.APPLIED
        await assert_rebuilt(world.repo, [CHESS], [VEGETARIAN])


class TestFactReviewService:
    async def test_a_review_run_that_revives_a_fact_kept_twice_rebuilds_the_cache_with_the_accounts_limits(self, worlds):
        world = ReviewWorld()
        await seed_background(world.repo)
        fact = await world.seed_stale(CHESS, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                      reported_date=NOW - timedelta(days=60), **subject())
        await world.seed_state(account_id=ACC, offers={fact: 2})
        plant(world.store.db)

        assert await ReviewRig(world).run() == {"status": "completed"}

        assert world.docs[fact]["state"] == "current"
        await assert_rebuilt(world.repo, [CHESS], [VEGETARIAN])


class TestFactLifecycleSweepService:
    async def test_a_sweep_that_stales_a_fact_rebuilds_the_cache_with_the_accounts_limits(self, worlds):
        world = JanitorWorld()
        await seed_background(world.repo)
        fact = await world.seed_due(LIVES, **subject())
        plant(world.store.db)

        result = await world.service.sweep()

        assert result["staled"] == 1 and world.docs[fact]["state"] == "stale"
        await assert_rebuilt(world.repo, *SUBJECT_GONE)


class TestFactProfileService:
    async def test_a_profile_run_that_writes_rebuilds_the_cache_with_the_accounts_limits(self):
        store, repo, bio = account_fact_world()
        await seed_background(repo)
        await repo.add_fact(make_fact(LIVES, **subject()))
        plant(store.db)
        clock = Clock()

        async def atlas() -> Tuple[PlaceNames, str]:
            return PlaceNames([ROME]), ATLAS_DIGEST

        profiles = FactProfileService(repo=repo, profiler=Profiler(), embeddings=Embedder(), atlas=atlas, clock=clock,
                                      limits=FactProfileRunLimits(max_per_run=20, call_budget_s=BUDGET, concurrency=4),
                                      bio_cache=bio)

        run = await profiles.run(ACC, clock.now + timedelta(seconds=3600) + LEASE_MARGIN)

        assert not run.error and run.written
        # A profile write changes no cached field: the rebuild shows as the planted copy giving way to the current facts.
        await assert_rebuilt(repo, [LIVES], [VEGETARIAN])
