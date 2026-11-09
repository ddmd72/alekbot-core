"""Blueprint.tail_categories: tokens of those categories leave their class section and render as a trailing
top-level block per class, after the dynamic parts and before the session key; a blueprint without the field is
unchanged. Also the Firestore document -> Blueprint load with and without the field."""
from unittest.mock import AsyncMock, Mock

import pytest

from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.system_fingerprint import render_session_key
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support.firestore_eval import EvalDb

KEY = "lamp-orbit-fern"
RULE_TEXT = "rule Output_Language_Fixed_EN (\n    speak English only\n)"
SPEAK_TEXT = "tone: calm"
OTHER_POLICY = "keep it short"


class _Allow(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(sanitized_text=text, risk_level=RiskLevel.SAFE, risk_score=0.0,
                                patterns_detected=[], action_taken="passed", metadata={})


def _tok(tid, class_, category, content):
    return Token(id=TokenId(tid), category=TokenCategory(category), class_=TokenClass(class_),
                 content=content, metadata={})


DOCS = {
    "SPEAK": _tok("SPEAK", "properties", "voice_style", SPEAK_TEXT),
    "POLICY_SHORT": _tok("POLICY_SHORT", "policies", "brevity", OTHER_POLICY),
    "LANG_RULE": _tok("LANG_RULE", "policies", "output_language", RULE_TEXT),
}


def _service(blueprint, tokens=("SPEAK", "POLICY_SHORT", "LANG_RULE")):
    blueprint_repo = AsyncMock()
    blueprint_repo.get = AsyncMock(return_value=blueprint)
    profile_repo = AsyncMock()
    profile_repo.get_agent_profile = AsyncMock(return_value=AgentProfile(
        blueprint_id=blueprint.id,
        tokens={t: ProfileToken(token_id=t, order=10 * (i + 1)) for i, t in enumerate(tokens)}))
    profile_repo.get_override_tokens = AsyncMock(return_value={})
    token_repo = AsyncMock()
    token_repo.get = AsyncMock(side_effect=lambda tid: DOCS.get(tid))
    bio = Mock()
    bio.format.return_value = ""
    return PromptAssemblyService(token_repo=token_repo, blueprint_repo=blueprint_repo, profile_repo=profile_repo,
                                 security_port=_Allow(), formatter=ContextFormatter(), bio_formatter=bio)


def _blueprint(**kw):
    return Blueprint(id="bp", outer_class="Alek extends Agent", class_order=["properties", "policies"], **kw)


async def _assemble(service, **kw):
    return await service.assemble(agent_type="voice", user_id="u", account_id="a", **kw)


def _policies_section(prompt):
    start = prompt.index("    policies {")
    return prompt[start:prompt.index("\n    }", start)]


@pytest.mark.asyncio
async def test_a_tail_token_leaves_its_section_and_renders_in_a_trailing_block_after_the_dynamic_parts():
    prompt = await _assemble(_service(_blueprint(tail_categories=["output_language"])),
                             include_datetime=True, query_specific_context="a note")

    assert "speak English only" not in _policies_section(prompt)
    assert OTHER_POLICY in _policies_section(prompt)
    tail_start = prompt.rindex("\npolicies {")
    assert tail_start > prompt.index("current_date_time {") and tail_start > prompt.index("query_specific_context")
    assert prompt.endswith("policies {\n    rule Output_Language_Fixed_EN (\n        speak English only\n    )\n}")
    assert prompt.count("Output_Language_Fixed_EN") == 1


@pytest.mark.asyncio
async def test_the_session_key_stays_last():
    prompt = await _assemble(_service(_blueprint(tail_categories=["output_language"])),
                             include_datetime=True, session_key=KEY)

    block = render_session_key(KEY)
    assert prompt.endswith(block)
    assert prompt.index("current_date_time {") < prompt.rindex("\npolicies {") < prompt.index(block)


@pytest.mark.asyncio
async def test_a_class_left_empty_by_the_tail_is_omitted_from_its_section():
    service = _service(_blueprint(tail_categories=["output_language"]), tokens=("SPEAK", "LANG_RULE"))
    prompt = await _assemble(service)

    assert "    policies {" not in prompt
    assert prompt.rindex("\npolicies {") > prompt.index("properties {")


@pytest.mark.asyncio
async def test_a_blueprint_without_tail_categories_keeps_every_token_in_its_section_and_has_no_trailing_block():
    prompt = await _assemble(_service(_blueprint()), include_datetime=True, session_key=KEY)

    assert RULE_TEXT.split("\n")[0] in _policies_section(prompt)
    assert "\npolicies {" not in prompt
    assert prompt.endswith(render_session_key(KEY))


@pytest.mark.asyncio
async def test_the_tail_survives_the_assembly_cache():
    service = _service(_blueprint(tail_categories=["output_language"]))
    first = await _assemble(service)
    second = await _assemble(service)

    assert first == second and "\npolicies {" in second


def _repo(doc):
    db = EvalDb()
    db.collection("bps").docs["bp"] = doc
    return FirestoreBlueprintRepository(db=db, collection_name="bps")


@pytest.mark.asyncio
async def test_the_loader_reads_tail_categories_and_defaults_to_empty():
    base = {"blueprint_id": "bp", "outer_class": "Alek extends Agent", "class_order": ["properties", "policies"]}

    assert (await _repo({**base, "tail_categories": ["output_language"]}).get("bp")).tail_categories == ["output_language"]
    assert (await _repo(base).get("bp")).tail_categories == []


@pytest.mark.asyncio
async def test_a_cache_hit_still_ends_with_the_tail_before_the_session_key_and_holds_the_rule_once():
    service = _service(_blueprint(tail_categories=["output_language"]))
    await _assemble(service, session_key=KEY)
    hit = await _assemble(service, session_key=KEY)

    assert hit.endswith(render_session_key(KEY))
    assert hit.rindex("\npolicies {") < hit.index("session_key {")
    assert hit.count("Output_Language_Fixed_EN") == 1


@pytest.mark.asyncio
async def test_the_internal_marker_never_reaches_a_final_prompt():
    for service in (_service(_blueprint(tail_categories=["output_language"])), _service(_blueprint())):
        for _ in range(2):
            assert "TAIL_BLOCK" not in await _assemble(service, include_datetime=True, session_key=KEY)


@pytest.mark.asyncio
async def test_invalidating_the_cache_between_two_assemblies_still_yields_the_tail():
    service = _service(_blueprint(tail_categories=["output_language"]))
    first = await _assemble(service)
    service.invalidate_cache()
    second = await _assemble(service)

    assert first == second and second.count("Output_Language_Fixed_EN") == 1 and "\npolicies {" in second
