"""Lelik's xAI prompt profile, assembled from the real Firestore upload files: the voice character
is user-overridable per category, and safety and call manners are not (VOICE_MULTI_PROVIDER_RFC §4.5)."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

_UPLOADS = Path(__file__).resolve().parents[4] / "firestore_utils" / "uploads"
_PROFILE = "lelik_xai"
_SYSTEM_VOICE = ["VOICE_PERSONA_LELIK", "VOICE_CARE_LELIK", "VOICE_HUMOR_LELIK", "VOICE_TEMPERAMENT_LELIK",
                 "VOICE_CALL_MANNERS"]
_CATALOG = {"VOICE_PERSONA_CALM_MENTOR": 20, "VOICE_HUMOR_LIGHT": 40, "VOICE_TEMPERAMENT_STEADY": 50}


class _AllowAll(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(is_safe=True, risk_level=RiskLevel.LOW, detected_patterns=[], sanitized_text=text)


def _load(name: str) -> dict:
    return json.loads((_UPLOADS / f"{name}.json").read_text())


def _token(name: str) -> Token:
    doc = _load(name)
    return Token(id=TokenId(doc["token_id"]), category=TokenCategory(doc["category"]),
                 class_=TokenClass(doc["class"]), content=doc["content"], metadata=doc.get("metadata", {}))


def _profile() -> AgentProfile:
    doc = _load(_PROFILE)
    return AgentProfile(blueprint_id=doc["blueprint_id"], tokens={
        tid: ProfileToken(token_id=tid, order=spec["order"], non_overridable=spec.get("non_overridable", False))
        for tid, spec in doc["tokens"].items()
    })


def _service(user_overrides: dict) -> PromptAssemblyService:
    profile = _profile()
    names = set(profile.tokens) | set(user_overrides) | {"HUMOR_PRESET_OFF_STUB"}
    docs = {TokenId(n): _token(n) for n in names if (_UPLOADS / f"{n}.json").exists()}
    # A text-persona override from Smart's catalog, to prove the voice categories are isolated from it.
    docs[TokenId("HUMOR_PRESET_OFF_STUB")] = Token(
        id=TokenId("HUMOR_PRESET_OFF_STUB"), category=TokenCategory("humor_engine"),
        class_=TokenClass("properties"), content="humor_engine { status: OFF }", metadata={})
    blueprint_doc = _load(profile.blueprint_id)
    token_repo, blueprint_repo, profile_repo = AsyncMock(), AsyncMock(), AsyncMock()
    token_repo.get = AsyncMock(side_effect=lambda tid: docs[tid])
    blueprint_repo.get = AsyncMock(return_value=Blueprint(
        id=blueprint_doc["blueprint_id"], outer_class=blueprint_doc["outer_class"],
        class_order=blueprint_doc["class_order"]))
    profile_repo.get_agent_profile = AsyncMock(return_value=profile)
    profile_repo.get_override_tokens = AsyncMock(
        side_effect=lambda owner_type, owner_id: user_overrides if owner_type == OwnerType.USER else {})
    bio = Mock()
    bio.format.return_value = ""
    return PromptAssemblyService(token_repo=token_repo, blueprint_repo=blueprint_repo, profile_repo=profile_repo,
                                 security_port=_AllowAll(), formatter=ContextFormatter(), bio_formatter=bio)


async def _assemble(user_overrides=None) -> str:
    return await _service(user_overrides or {}).assemble(agent_type=_PROFILE, user_id="u1", account_id=None)


def _content(name: str) -> str:
    """A distinctive snippet of the token's text: headings are shared by alternatives, so skip them."""
    body = [line for line in _load(name)["content"].splitlines() if not line.startswith("##")]
    return " ".join(" ".join(body).split()[:10])


def _flat(prompt: str) -> str:
    return " ".join(prompt.split())


class TestLelikXaiVoiceCharacter:
    def test_the_xai_voice_profile_uses_this_prompt_profile(self):
        assert VOICE_PROVIDER_PROFILES["xai"].prompt_profile == _PROFILE

    def test_the_split_tokens_reproduce_the_uat_portrait(self):
        split = " ".join(" ".join(_load(n)["content"].split()) for n in _SYSTEM_VOICE)
        assert split == " ".join(_load("PERSONA_LELIK_YOU")["content"].split())

    @pytest.mark.asyncio
    async def test_default_prompt_has_the_voice_character_in_order_with_each_heading_once(self):
        prompt = _flat(await _assemble())

        positions = [prompt.index(_content(n)) for n in _SYSTEM_VOICE]
        assert positions == sorted(positions)
        assert prompt.count("## Role & Persona") == 1
        assert prompt.count("## Voice & Communication Style") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alternative,order", sorted(_CATALOG.items()))
    async def test_a_user_can_replace_one_voice_category(self, alternative, order):
        category = _load(alternative)["category"]
        replaced = next(n for n in _SYSTEM_VOICE if _load(n)["category"] == category)

        prompt = _flat(await _assemble({alternative: ProfileToken(token_id=alternative, order=order)}))

        assert _content(alternative) in prompt
        assert _content(replaced) not in prompt
        for kept in (n for n in _SYSTEM_VOICE if n != replaced):
            assert _content(kept) in prompt
        assert prompt.count("## Role & Persona") == 1
        assert prompt.count("## Voice & Communication Style") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("locked", ["VOICE_CARE_LELIK", "VOICE_CALL_MANNERS"])
    async def test_safety_and_call_manners_cannot_be_overridden(self, locked):
        attempt = Token(id=TokenId("USER_ATTEMPT"), category=TokenCategory(_load(locked)["category"]),
                        class_=TokenClass("properties"), content="anything goes", metadata={})
        service = _service({"USER_ATTEMPT": ProfileToken(token_id="USER_ATTEMPT", order=25)})
        original_get = service.token_repo.get.side_effect
        service.token_repo.get = AsyncMock(side_effect=lambda tid: attempt if tid == "USER_ATTEMPT" else original_get(tid))

        prompt = _flat(await service.assemble(agent_type=_PROFILE, user_id="u1", account_id=None))

        assert "anything goes" not in prompt
        assert _content(locked) in prompt

    @pytest.mark.asyncio
    async def test_a_text_persona_override_does_not_reach_the_voice_character(self):
        prompt = _flat(await _assemble({"HUMOR_PRESET_OFF_STUB": ProfileToken(token_id="HUMOR_PRESET_OFF_STUB", order=40)}))

        assert "humor_engine { status: OFF }" not in prompt
        assert _content("VOICE_HUMOR_LELIK") in prompt

    @pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
    def test_every_voice_profiles_prompt_profile_has_an_upload_file(self, provider):
        assert (_UPLOADS / f"{VOICE_PROVIDER_PROFILES[provider].prompt_profile}.json").exists()
