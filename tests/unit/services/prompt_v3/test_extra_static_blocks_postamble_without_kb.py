"""PromptAssemblyService: extra static blocks follow the template when there is no knowledge base and no preamble
(the fact profiler's enum block). They were dropped on that branch before; the preamble and knowledge-base branches are
unchanged. In-memory repos; assertions on the returned string only."""
from unittest.mock import Mock

import pytest

from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

OUTER = "Probe extends Agent"
BLOCK_A = "FIRST BLOCK:\nrow one\nrow two"
BLOCK_B = "SECOND BLOCK line"


class _Tokens:
    def __init__(self, doc): self.doc = doc

    async def get(self, token_id): return self.doc


class _Blueprints:
    async def get(self, blueprint_id): return Blueprint(id="bp", outer_class=OUTER, class_order=["policies"])


class _Profiles:
    async def get_agent_profile(self, agent_id):
        return AgentProfile(blueprint_id="bp", tokens={"POLICY_BODY": ProfileToken("POLICY_BODY", 10)})

    async def get_override_tokens(self, owner_type, owner_id): return {}


def _service() -> PromptAssemblyService:
    security = Mock()

    def _result(text, **_):
        result = Mock()
        result.sanitized_text = text
        result.risk_level = Mock(value="LOW")
        return result

    security.validate = Mock(side_effect=_result)
    from unittest.mock import AsyncMock
    security.validate = AsyncMock(side_effect=_result)
    bio = Mock()
    bio.format = Mock(return_value="")
    bio.format_directives = Mock(return_value="")
    doc = Token(id=TokenId("POLICY_BODY"), category=TokenCategory("body"), class_=TokenClass("policies"),
                content="be brief", metadata={})
    return PromptAssemblyService(token_repo=_Tokens(doc), blueprint_repo=_Blueprints(), profile_repo=_Profiles(),
                                 security_port=security, formatter=ContextFormatter(), bio_formatter=bio)


async def _assemble(**kw):
    return await _service().assemble(agent_type="probe", user_id=None, account_id=None, **kw)


@pytest.mark.asyncio
async def test_blocks_follow_the_template_in_order_before_the_cache_boundary():
    plain = await _assemble(kb_preamble=False)
    out = await _assemble(kb_preamble=False, extra_static_blocks=[BLOCK_A, BLOCK_B])
    static = out.split(PROMPT_CACHE_BOUNDARY)[0]
    assert plain.split(PROMPT_CACHE_BOUNDARY)[0].strip() in static
    assert static.index(f"class {OUTER} {{") < static.index(BLOCK_A) < static.index(BLOCK_B)
    assert BLOCK_A + "\n\n" + BLOCK_B in static
    assert "knowledge_base {" not in out


@pytest.mark.asyncio
async def test_no_blocks_leave_the_prompt_unchanged():
    assert await _assemble(extra_static_blocks=[]) == await _assemble(extra_static_blocks=None) == await _assemble()


@pytest.mark.asyncio
async def test_the_preamble_branch_still_puts_the_blocks_before_the_class():
    out = await _assemble(kb_preamble=True, extra_static_blocks=[BLOCK_A])
    assert out.index(BLOCK_A) < out.index(f"class {OUTER} {{")
