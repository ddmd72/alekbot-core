from unittest.mock import AsyncMock, Mock

import pytest

from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

CATALOG = "available_skills {\n    - flight-status — Use when a flight is asked about.\n}"


def _service():
    security = Mock()
    security.validate = AsyncMock(side_effect=lambda text, **_: ValidationResult(
        sanitized_text=text, risk_level=RiskLevel.SAFE, risk_score=0.0,
        patterns_detected=[], action_taken="passed", metadata={}))
    bio = Mock()
    bio.format = Mock(side_effect=lambda facts: "\n".join(f"- {f['text']}" for f in facts))
    bio.format_directives = Mock(side_effect=lambda ds: "\n".join(f"- {d['text']}" for d in ds))
    return PromptAssemblyService(
        token_repo=Mock(), blueprint_repo=Mock(), profile_repo=Mock(),
        security_port=security, formatter=Mock(), bio_formatter=bio,
    )


async def _inject(service, **kw):
    return await service._inject_runtime_context(
        prompt="class Prompt {}", biographical_facts=[], conversation_history=[],
        user_id="u", **kw,
    )


@pytest.mark.asyncio
async def test_catalog_before_directives_and_boundary():
    out = await _inject(_service(), directives=[{"text": "Never guess."}], skills_catalog=CATALOG)
    assert out.index("available_skills {") < out.index("standing_directives {") < out.index(PROMPT_CACHE_BOUNDARY)


@pytest.mark.asyncio
async def test_catalog_before_boundary_without_directives():
    out = await _inject(_service(), skills_catalog=CATALOG)
    assert out.index("available_skills {") < out.index(PROMPT_CACHE_BOUNDARY)


@pytest.mark.asyncio
async def test_no_catalog_no_block():
    assert "available_skills" not in await _inject(_service(), skills_catalog=None)


@pytest.mark.asyncio
async def test_catalog_is_not_revalidated():
    service = _service()
    await _inject(service, skills_catalog=CATALOG)
    contexts = [c.kwargs.get("context", "") for c in service.security_port.validate.call_args_list]
    assert not any("skill" in c for c in contexts)


@pytest.mark.asyncio
async def test_prompt_builder_forwards_catalog():
    assembly = Mock()
    assembly.assemble = AsyncMock(return_value="PROMPT")
    builder = PromptBuilder(repo=None, assembly_service=assembly)
    await builder.build_for_agent(agent_type="smart", include_biographical=False, skills_catalog=CATALOG)
    assert assembly.assemble.await_args.kwargs["skills_catalog"] == CATALOG
