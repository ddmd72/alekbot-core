"""P13 card F: the prompt and the enrichment through a real Smart and a real Router.

From an entry path (`/worker` through the real `UserNotificationService`, or a chat turn through the real
`ConversationHandler`) via the real coordinator to a **real `SmartResponseAgent`** (and, on a chat turn, a **real
`RouterAgent`**) over scripted models. Real between them: `PromptBuilder` over the snapshot prompts seeded into the
world's Firestore fake (`seed_agent_prompt`), `TaskExecutionResolver` over `AgentContextBuilder` and a `ProviderRegistry`
holding Smart's scripted model under every provider name, and `SearchEnrichmentService` over the fact store. The
boundary asserted on is the request each scripted model received: what reached the system prompt (before or after
`PROMPT_CACHE_BOUNDARY`), the model the tier resolved to (`TieredLLM` names it `tier-<tier>`; the execution context's
own model is `scripted-model`, so "no override resolved" and "a tier resolved" differ) and the thinking effort.

Pins P13-D3: the daily review asks for `thinking_effort="medium"`, the accepted complexity RFC says that value wins, and
Smart lets the complexity's own `"high"` win instead.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.core.router_agent import RouterAgent, create_router_agent
from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.domain.entities import FactDomain
from src.domain.messaging import MessageContext
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserBotConfig
from src.handlers import worker_handler
from src.infrastructure.task_execution_resolver import TaskExecutionResolver
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.prompt_builder import PromptBuilder
from src.services.provider_registry import ProviderRegistry
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.clock_instants import pin_datetime
from tests.support.fact_lifecycle_world import seed_fact
from tests.support.fact_store import FactStore
from tests.support.prompt_seed import seed_agent_prompt
from tests.support.redelivery_world import ScriptedReview
from tests.support.scripted_llm import ScriptedLLM, execution_context, text
from tests.support.seam_world import ACCOUNT, OTHER_SLACK, PRIMARY, T0, USER, SeamWorld, require
from tests.support.tool_e2e import smart_rig

EMAILS = [
    {"email_id": "m1", "from": "user@example.com", "subject": "Your flight booking AAA→BBB",
     "date": "2026-10-26T18:00:00+00:00", "snippet": "", "body": "", "attachments": []},
    {"email_id": "m2", "from": "user@example.com", "subject": "Example School parents' meeting",
     "date": "2026-10-26T19:00:00+00:00", "snippet": "", "body": "", "attachments": []},
]
SUBJECTS = [e["subject"] for e in EMAILS]
FACT = "Alex's blood type is A Rh+."
QUESTION = "What is my blood type?"
D3 = ("P13-D3: the daily review's thinking_effort='medium' is overridden by the complexity's 'high' in Smart, while the "
      "accepted RFC TASK_COMPLEXITY_EXECUTION_SETTINGS says the context's thinking_effort has priority "
      "(smart_response_agent.py:389, override wins); fix after the plan (owner)")


class TieredLLM(ScriptedLLM):
    """`ScriptedLLM` whose tier resolves to a model named after it, so the request names the tier that ran."""

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return f"tier-{tier.value}"


class ChatChannel:
    """The platform response channel of one chat turn, re-declared from card D (private to its file): the methods
    `ConversationHandler` calls on a turn (`test_conversation_handler_interactive_delivery.py:31-44`), recording what
    was sent."""

    def __init__(self, platform: str, channel_id: str) -> None:
        self.platform = platform
        self.channel_id = channel_id
        self.max_message_length = 4000
        self.supports_message_editing = True
        self.texts: List[str] = []
        self.statuses: List[Any] = []
        self.rich: List[Any] = []

    async def send_status_with_phrase(self, status_type: Any, thread_id: Optional[str] = None) -> Tuple[str, str]:
        self.statuses.append(status_type)
        return "m1", "thinking"

    async def get_status_phrase(self, status_type: Any) -> str:
        return "processing"

    async def update_status_with_phrase_and_dots(self, message_id: str, phrase: str, dots: int) -> None:
        return None

    async def send_status(self, status: Any, thread_id: Optional[str] = None) -> None:
        self.statuses.append(status)

    async def send_message(self, text: str, thread_id: Optional[str] = None) -> None:
        self.texts.append(text)

    async def send_chunked_message(self, text: str, status_message_id: Optional[str] = None,
                                   thread_id: Optional[str] = None, link_list: Optional[list] = None) -> None:
        self.texts.append(text)

    async def send_flat_response(self, text: str, status_message_id: Optional[str] = None) -> None:
        self.texts.append(text)

    async def update_message(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def send_rich_content(self, content: Any, thread_id: Optional[str] = None) -> None:
        self.rich.append(content)


def envelope(full: str, summary: str) -> Any:
    """Smart's output envelope (`tests/support/agent_answer_fakes.py:8-17`), as its scripted model answers it."""
    return text(json.dumps({"full_response": full, "response_summary": summary, "rich_content": None,
                            "link_list": []}))


def triage(search_intent: str) -> Any:
    """The Router's triage answer (`TRIAGE_RESPONSE_SCHEMA`, `router_agent.py:139-170`) for the blood-type question."""
    return text(json.dumps({
        "needs_memory_search": True, "reasoning": "personal", "search_intent": search_intent,
        "relevant_domains": ["health"], "semantic_lens": ["blood type"], "search_phrase": "blood type",
        "metadata": {"user_tone": "friendly", "task_complexity": "info_search"},
    }))


def resolver_over(llm: TieredLLM) -> TaskExecutionResolver:
    """The real resolver; the registry holds `llm` under every provider Smart's strategy allows
    (`agent_context_builder.py:44-49`), so whichever one `UserBotConfig()` resolves to (`gemini`) is the scripted one."""
    registry: ProviderRegistry = ProviderRegistry()
    for name in AgentProviderStrategy.get_strategy("smart")["allowed_providers"]:
        registry.register(name, llm)
    return TaskExecutionResolver(AgentContextBuilder(registry, InMemoryProviderResilience()))


def split(request: Any) -> Tuple[str, str]:
    """A request's system prompt as (static part, dynamic part) around `PROMPT_CACHE_BOUNDARY`."""
    static, boundary, dynamic = request.system_instruction.partition(PROMPT_CACHE_BOUNDARY)
    require(boundary == PROMPT_CACHE_BOUNDARY, "the system prompt carries the cache boundary")
    return static, dynamic


def scripted_out(llm: ScriptedLLM) -> bool:
    return llm.unanswered == 0 and llm.pending() == 0


async def seam(monkeypatch: Any, *smart_script: Any, router_script: Optional[List[Any]] = None,
               **worker_overrides: Any) -> SimpleNamespace:
    """The seam world with a real Smart (and, given `router_script`, a real Router) registered in its coordinator.
    Primary channel `("slack", "C1")`. The prompt builder's repository is the fact store's; Smart gets no repository,
    so the only way a fact reaches its prompt is the Router's enrichment (`smart_response_agent.py:427-432`)."""
    world = SeamWorld(monkeypatch, **worker_overrides)
    await world.set_primary(*PRIMARY)
    store = FactStore()
    await seed_agent_prompt(world.db, world.env, "smart")
    builder = PromptBuilder(store.repo, assembly_service=build_prompt_assembly_service(world.db, world.env)[0])
    smart_llm = TieredLLM(list(smart_script))
    smart = smart_rig(smart_llm, coordinator=world.coordinator, prompt_builder=builder,
                      resolver=resolver_over(smart_llm), user_config=UserBotConfig(), session_store=world.sessions)
    world.coordinator.register_agent(smart)
    router_llm: Optional[ScriptedLLM] = None
    if router_script is not None:
        await seed_agent_prompt(world.db, world.env, "router")
        router_llm = ScriptedLLM(router_script)
        router: RouterAgent = create_router_agent(
            execution_context=execution_context(router_llm, "router"), coordinator=world.coordinator,
            smart_agent_id=f"smart_response_agent_{USER}", user_id=USER, session_store=world.sessions,
            repository=None, search_enrichment_service=SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20),
            prompt_builder=builder, notes_port=None,
        )
        world.coordinator.register_agent(router)
    return SimpleNamespace(world=world, store=store, smart_llm=smart_llm, router_llm=router_llm)


def _slack_turn(text_: str) -> MessageContext:
    """As `src/adapters/slack/http_adapter.py:512-530` builds it, without `turn_id` (card D's shape)."""
    return MessageContext(text=text_, session_id="u1:C2", user_id=USER, account_id=ACCOUNT,
                          metadata={"channel": "C2", "event_type": "message"})


async def _daily_review(monkeypatch: Any) -> SimpleNamespace:
    s = await seam(monkeypatch, envelope("Your inbox review is ready.", "Reviewed the inbox."),
                   email_review=ScriptedReview(EMAILS))
    pin_datetime(monkeypatch, worker_handler, T0)   # the alert's date is read from real time (worker_handler.py:1190)
    s.answer = await s.world.run({"task_type": "daily_email_review", "user_id": USER, "account_id": ACCOUNT})
    return s


# ----------------------------------------------------------------------------- the daily review

async def test_a_daily_review_reaches_smart_s_static_prompt_and_runs_at_the_reasoning_tier(monkeypatch):
    """Guards `email_for_triage` `worker_handler.py:1204` → `ums.py:391` → `smart_response_agent.py:455-462` →
    `prompt_builder.py:220` → `prompt_assembly_service.py:469-470` (before the boundary, `:542`); `task_complexity`
    `worker_handler.py:1203` → `ums.py:390` → `smart_response_agent.py:379` → `task_execution_resolver.py:73-109`
    (PERFORMANCE) → `smart_response_agent.py:547` (`model_name`). Dropping `email_for_triage` leaves the subjects out of
    the prompt; dropping `task_complexity` leaves the model at `scripted-model`."""
    s = await _daily_review(monkeypatch)

    require(not s.world.unscripted(), "every agent call was scripted")
    require(scripted_out(s.smart_llm), "Smart's script was used exactly")
    assert s.answer == ({"status": "ok", "emails": 2}, 200)
    assert [p.value for p in s.world.posts_to(*PRIMARY)] == ["Your inbox review is ready."]
    [request] = s.smart_llm.requests
    static, dynamic = split(request)
    assert "email_for_triage {" in static and all(subject in static for subject in SUBJECTS)
    assert "email_for_triage" not in dynamic and not any(subject in dynamic for subject in SUBJECTS)
    assert request.model_name == "tier-performance"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=D3)
async def test_a_daily_review_runs_at_the_thinking_effort_it_asks_for(monkeypatch):
    """Pins P13-D3. Guards `thinking_effort` `worker_handler.py:1202` → `ums.py:389` → `smart_response_agent.py:381`
    (read) → `:389` (dropped when the complexity override sets its own effort) → `:552`.
    The precondition `model_name == "tier-performance"` holds before and after the fix (the override still resolves).
    Flipping fix, `smart_response_agent.py:389`: `thinking = message.context.get("thinking_effort") or
    (override.thinking_effort if override.thinking_effort is not None else ctx_thinking)`."""
    s = await _daily_review(monkeypatch)

    require(not s.world.unscripted(), "every agent call was scripted")
    require(scripted_out(s.smart_llm), "Smart's script was used exactly")
    require(s.answer == ({"status": "ok", "emails": 2}, 200), "the review was delivered")
    require(len(s.smart_llm.requests) == 1, "exactly one Smart request")
    [request] = s.smart_llm.requests
    require(request.model_name == "tier-performance", "the deep_reasoning override resolved")
    assert request.thinking == "medium"


async def test_a_reminder_without_an_effort_runs_at_its_complexity_s_effort(monkeypatch):
    """Control for P13-D3: no context effort, so the complexity's own `"high"` is the effort today and after the fix.
    Guards `task_complexity` `worker_handler.py:1014` → `ums.py:390` → `smart_response_agent.py:379` (tier) and `:389`
    → `:552` (the override's effort). Dropping `task_complexity` leaves `scripted-model` and no effort."""
    s = await seam(monkeypatch, envelope("Time to call dad.", "Reminded the user to call dad."))
    note_id = await s.world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1),
                                      complexity=TaskComplexity.DEEP_REASONING)

    answer = await s.world.run({"task_type": "execute_reminder", "note_id": note_id, "user_id": USER,
                                "due_at": T0.isoformat()})

    require(not s.world.unscripted(), "every agent call was scripted")
    require(scripted_out(s.smart_llm), "Smart's script was used exactly")
    assert answer == ({"status": "ok"}, 200)
    assert [p.value for p in s.world.posts_to(*PRIMARY)] == ["Time to call dad."]
    [request] = s.smart_llm.requests
    assert request.model_name == "tier-performance"
    assert request.thinking == "high"


# ----------------------------------------------------------------------------- the chat turn

async def test_a_chat_turn_s_enriched_fact_reaches_smart_after_the_cache_boundary(monkeypatch):
    """Guards `semantic_lens`/`search_phrase`/`relevant_domains` `router_agent.py:332-338` → the fact store; the
    routed `enriched_context` `:385` → `smart_response_agent.py:419,442-445` → `prompt_builder.py:191-201,213-214` →
    `prompt_assembly_service.py:409-417,540-542` (after the boundary); the routed `task_complexity` `router_agent.py:384`
    → `smart_response_agent.py:379` → resolver (BALANCED). Dropping `enriched_context` loses the fact; putting it with
    the static facts moves it before the boundary; dropping `task_complexity` leaves `scripted-model`."""
    s = await seam(monkeypatch, envelope("Your blood type is A Rh+.", "Told the user their blood type."),
                   router_script=[triage("topic")])
    await seed_fact(s.store.repo, FACT, account=ACCOUNT, created_by_user_id=USER, domain=FactDomain.HEALTH)
    chat = ChatChannel(*OTHER_SLACK)

    await s.world.chat_handler().handle_message(_slack_turn(QUESTION), chat)

    require(not s.world.unscripted(), "every agent call was scripted")
    require(scripted_out(s.router_llm) and scripted_out(s.smart_llm), "both scripts were used exactly")
    assert len(s.router_llm.requests) == 1           # triage ran on the scripted model, not the rule-based fallback
    assert chat.texts == ["Your blood type is A Rh+."]
    [request] = s.smart_llm.requests
    static, dynamic = split(request)
    assert "query_specific_context" in dynamic and f"- {FACT}" in dynamic.splitlines()
    assert FACT not in static
    assert request.model_name == "tier-balanced"


async def test_a_chat_turn_that_needs_no_search_carries_no_enriched_fact(monkeypatch):
    """Control for the test above: with `search_intent == "none"` the Router skips enrichment (`router_agent.py:314`),
    and with no repository Smart reads no biographical cache (`smart_response_agent.py:427-432`), so the fact is nowhere
    in Smart's prompt. Makes the enrichment test non-vacuous: the fact reaches the prompt only through enrichment."""
    s = await seam(monkeypatch, envelope("I do not know your blood type.", "Did not know the blood type."),
                   router_script=[triage("none")])
    await seed_fact(s.store.repo, FACT, account=ACCOUNT, created_by_user_id=USER, domain=FactDomain.HEALTH)
    chat = ChatChannel(*OTHER_SLACK)

    await s.world.chat_handler().handle_message(_slack_turn(QUESTION), chat)

    require(not s.world.unscripted(), "every agent call was scripted")
    require(scripted_out(s.router_llm) and scripted_out(s.smart_llm), "both scripts were used exactly")
    require(len(s.router_llm.requests) == 1, "triage ran on the scripted model")
    assert chat.texts == ["I do not know your blood type."]
    [request] = s.smart_llm.requests
    assert "query_specific_context" not in request.system_instruction
    assert FACT not in request.system_instruction
