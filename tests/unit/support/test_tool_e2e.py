"""The Smart rig (P14 card A): one real turn over the scripted model, the three local-tool gates as production sets
them, and a given collaborator is the one Smart uses."""
import inspect
from types import SimpleNamespace

import pytest

from src.adapters.firestore_memory_fact_draft_repository import FirestoreMemoryFactDraftRepository
from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.adapters.security.composite_adapter import CompositeAdapter
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentStatus
from src.domain.skill import Skill
from src.domain.user import UserBotConfig
from src.services.memory_fact_service import MemoryFactService
from src.services.skill_service import SkillService
from src.services.workspace_service import WorkspaceService
from tests.support.firestore_eval import EvalDb
from tests.support.gcs_fake import FakeGcs
from src.agents.core.smart_response_agent import SmartResponseAgent
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import EmptySessions, StubPromptBuilder, smart_message, smart_rig

BUCKET = "ws-bucket"
SKILL = Skill(name="morning-briefing", description="Use when the owner asks for the morning briefing.",
              body="Collect the Springfield news and summarise them in five lines.")


def _skills() -> SkillService:
    return SkillService(FirestoreSkillRepository(EvalDb(), EnvironmentConfig()),
                        CompositeAdapter([RegexSecurityAdapter()]), system_skills=[SKILL])


def _memory() -> MemoryFactService:
    return MemoryFactService(FirestoreMemoryFactDraftRepository(EvalDb(), EnvironmentConfig()),
                             CompositeAdapter([RegexSecurityAdapter()]), EmptySessions())


def _workspace() -> WorkspaceService:
    return WorkspaceService(GcsWorkspaceAdapter(BUCKET, client=FakeGcs(buckets={BUCKET: "versioned"}).client()))


def _declared(llm: ScriptedLLM) -> set:
    return {t["name"] for t in llm.requests[0].tools or []}


async def test_one_turn_runs_and_consumes_the_script():
    llm = ScriptedLLM([text("done")])
    agent = smart_rig(llm)

    response = await agent.execute(smart_message("What is on today?"))

    assert response.status == AgentStatus.SUCCESS
    assert "done" in str(response.result)
    assert len(llm.requests) == 1
    assert llm.unanswered == 0 and llm.pending() == 0


@pytest.mark.parametrize("given", [True, False])
async def test_workspace_is_declared_iff_a_workspace_service_is_given(given):
    llm = ScriptedLLM([text("done")])
    agent = smart_rig(llm, workspace_service=_workspace() if given else None)

    await agent.execute(smart_message("Open the garden shed notes", interactive=False, run_origin="chat"))

    assert ("workspace" in _declared(llm)) is given
    assert llm.unanswered == 0 and llm.pending() == 0


@pytest.mark.parametrize("given, interactive", [(True, True), (True, False), (False, True)])
async def test_propose_memory_facts_is_declared_iff_a_memory_service_and_an_interactive_turn(given, interactive):
    llm = ScriptedLLM([text("done")])
    agent = smart_rig(llm, memory_fact_service=_memory() if given else None)

    await agent.execute(smart_message("Remember the passport expiry", interactive=interactive))

    assert ("propose_memory_facts" in _declared(llm)) is (given and interactive)
    assert llm.unanswered == 0 and llm.pending() == 0


@pytest.mark.parametrize("given, interactive", [(True, True), (True, False), (False, True)])
async def test_draft_skill_is_declared_iff_a_skill_service_with_a_skill_and_an_interactive_turn(given, interactive):
    llm = ScriptedLLM([text("done")])
    agent = smart_rig(llm, skill_service=_skills() if given else None)

    await agent.execute(smart_message("Write a skill for the morning briefing", interactive=interactive))

    assert ("draft_skill" in _declared(llm)) is (given and interactive)
    assert ("use_skill" in _declared(llm)) is given
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_given_prompt_builder_reaches_the_request_and_given_collaborators_are_held():
    llm = ScriptedLLM([text("done")])
    builder = StubPromptBuilder(prompt="P13 PROMPT")
    resolver = SimpleNamespace(resolve=lambda context, config: None, next_provider_override=lambda **kw: None)
    config = UserBotConfig(timezone="Europe/Berlin")

    agent = smart_rig(llm, prompt_builder=builder, resolver=resolver, user_config=config)
    await agent.execute(smart_message("What is on today?"))

    assert llm.requests[0].system_instruction == "P13 PROMPT"
    assert builder.calls and builder.calls[0]["agent_type"] == "smart"
    assert agent.resolver is resolver and agent.user_config is config
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_multi_turn_smart_run_records_each_turns_own_tool_result():
    """Smart's engine builds every turn over one shared message list: each request must read as it was sent."""
    llm = ScriptedLLM([
        tools(("workspace", {"command": "create", "path": "projects/garden-shed.md",
                             "content": "Planning permission for a garden shed: decision expected by 2026-10-30."})),
        tools(("workspace", {"command": "view", "path": "projects/garden-shed.md"})),
        text("done"),
    ])
    agent = smart_rig(llm, workspace_service=_workspace())

    await agent.execute(smart_message("Note the garden shed permission in the workspace"))

    found = tool_results(llm)
    assert [n for n, _ in found] == ["workspace", "workspace"]
    assert str(found[0][1]).startswith("created workspace:"), found[0][1]
    assert llm.unanswered == 0 and llm.pending() == 0


# Every keyword smart_rig passes through, and the attribute Smart keeps it under (smart_response_agent.py __init__).
PASSTHROUGH = [
    ("session_store", "session_store"), ("repository", "repository"), ("embedding_service", "embedding_service"),
    ("coordinator", "coordinator"), ("history_summary_service", "history_summary_service"),
    ("long_turn_registry", "long_turn_registry"), ("execution_context", "execution_context"),
    ("thinking_effort", "_default_thinking_effort"), ("user_timezone", "_user_timezone"),
    ("history_recent_full_turns", "history_recent_full_turns"), ("model_name", "model_name"),
    ("prompt_builder", "prompt_builder"), ("resolver", "resolver"), ("user_config", "user_config"),
    ("skill_service", "skill_service"), ("memory_fact_service", "memory_fact_service"),
    ("workspace_service", "workspace_service"),
]


@pytest.mark.parametrize("keyword, attribute", PASSTHROUGH)
def test_every_given_keyword_reaches_the_agent(keyword, attribute):
    llm = ScriptedLLM([])
    sentinels = {"execution_context": execution_context(llm, "smart", model="given-model"),
                 "thinking_effort": "high", "user_timezone": "Europe/Helsinki", "history_recent_full_turns": 9,
                 "model_name": "given-model"}
    given = sentinels.get(keyword, object())

    agent = smart_rig(llm, **{keyword: given})

    assert getattr(agent, attribute) is given


def test_smart_rig_takes_exactly_smarts_constructor_keywords():
    rig_keywords = set(inspect.signature(smart_rig).parameters) - {"llm", "user_id"}
    smart_keywords = set(inspect.signature(SmartResponseAgent.__init__).parameters) - {"self", "config"}
    assert rig_keywords == smart_keywords
    assert {k for k, _ in PASSTHROUGH} == smart_keywords


def test_smart_message_merges_the_given_context():
    assert smart_message("x", run_origin="voice:ask_alek").context["run_origin"] == "voice:ask_alek"
