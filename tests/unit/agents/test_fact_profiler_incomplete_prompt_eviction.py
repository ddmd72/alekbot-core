"""An incomplete fact profiler prompt is not served again once its tokens exist: the agent drops exactly its own
assembled entry from the shared PromptAssemblyService cache (not the whole cache). Real PromptBuilder and assembly
service over in-memory repos; a token missing from the repo assembles silently without its section."""
from unittest.mock import AsyncMock, Mock

from src.agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent
from src.domain.agent import AgentConfig
from src.domain.fact_profiler_reply import ProfilerStatus
from src.domain.prompt import ANONYMOUS_ACCOUNT_ID, ANONYMOUS_USER_ID
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, reply
from tests.unit.agents.test_fact_profiler_agent import ANSWER, TEXT

COG, OUT = "COGNITIVE_PROCESS_FACT_PROFILER", "OUTPUT_FORMAT_FACT_PROFILER"


def _token(token_id, cls):
    return Token(id=TokenId(token_id), category=TokenCategory(cls), class_=TokenClass(cls), content="a rule",
                 metadata={})


class _Tokens:
    def __init__(self, docs): self.docs = docs

    async def get(self, token_id): return self.docs[str(token_id)]


class _Blueprints:
    async def get(self, blueprint_id):
        return Blueprint(id="bp", outer_class="FactProfilerAgent extends Agent",
                         class_order=["cognitive_process", "output_format"])


class _Profiles:
    async def get_agent_profile(self, agent_id):
        return AgentProfile(blueprint_id="bp", tokens={COG: ProfileToken(COG, 10), OUT: ProfileToken(OUT, 10)})

    async def get_override_tokens(self, owner_type, owner_id): return {}


def _assembly(docs):
    security = Mock()
    security.validate = AsyncMock(side_effect=lambda text, **_: Mock(sanitized_text=text, risk_level=Mock(value="LOW")))
    bio = Mock()
    bio.format = Mock(return_value="")
    bio.format_directives = Mock(return_value="")
    return PromptAssemblyService(token_repo=_Tokens(docs), blueprint_repo=_Blueprints(), profile_repo=_Profiles(),
                                 security_port=security, formatter=ContextFormatter(), bio_formatter=bio)


async def test_an_incomplete_prompt_is_not_served_again_after_the_tokens_appear():
    docs = {COG: _token(COG, "cognitive_process")}          # the output format is not uploaded yet
    assembly = _assembly(docs)
    other_key = assembly._build_cache_key("smart", "acc-1", "usr-1")
    assembly._save_to_cache(other_key, "another agent's template")
    llm = ScriptedLLM([reply(ANSWER)])
    agent = FactProfilerAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE),
                              context(llm, "claude-sonnet-5-5"), PromptBuilder(repo=None, assembly_service=assembly))

    first = await agent.profile("acc-1", TEXT, None)

    assert first.status is ProfilerStatus.UNAVAILABLE and llm.requests == []
    assert assembly._get_from_cache(assembly._build_cache_key(AGENT_TYPE, ANONYMOUS_ACCOUNT_ID, ANONYMOUS_USER_ID)) is None
    assert assembly._get_from_cache(other_key) == "another agent's template"   # only this agent's entry went

    docs[OUT] = _token(OUT, "output_format")                 # the owner uploads the token
    second = await agent.profile("acc-1", TEXT, None)

    assert second.status is ProfilerStatus.OK
    assert "output_format {" in llm.requests[0].system_instruction


def test_the_keyed_eviction_reports_whether_it_dropped_an_entry():
    assembly = _assembly({})
    key = assembly._build_cache_key("fact_profiler", None, None)
    assembly._save_to_cache(key, "x")
    assert assembly.invalidate_assembled("fact_profiler") is True
    assert assembly.invalidate_assembled("fact_profiler") is False
