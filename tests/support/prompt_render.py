"""Render a profile of `prompts_snapshot/` with the production assembler, offline.

Through the public `PromptAssemblyService.assemble()`, the call production makes (`build_for_agent` -> `assemble`), as
`tests/unit/services/prompt_v3/test_lelik_xai_voice_character.py` does: an allow-all `SecurityPort`, the real
`ContextFormatter`, a stub biographical formatter. The repositories are in-memory readers of the snapshot files; no
Firestore, no overrides, no runtime context. `render_static(agent_id)` is the static template, the text before
`PROMPT_CACHE_BOUNDARY`; `rendered_token_ids` says which token files went into it."""
import asyncio
from typing import Dict, List
from unittest.mock import Mock

from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support import prompt_vocabulary as pv


class _AllowAll(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(is_safe=True, risk_level=RiskLevel.LOW, detected_patterns=[], sanitized_text=text)


class _Tokens:
    def __init__(self) -> None:
        self.read: List[str] = []

    async def get(self, token_id: TokenId) -> Token:
        path = pv.token_path(token_id)
        if not path.exists():  # the "user"-namespace tokens (system-level too, `prompts_snapshot/README.md`)
            path = pv.SNAPSHOT / "tokens" / "user" / f"{token_id}.groovy"
        doc = pv.ser.token_from_file(path.read_text(encoding="utf-8"))
        self.read.append(str(token_id))
        return Token(
            id=TokenId(doc["token_id"]), category=TokenCategory(doc["category"]), class_=TokenClass(doc["class"]),
            content=doc["content"], metadata=doc.get("metadata") or {},
        )


class _Blueprints:
    async def get(self, blueprint_id: str) -> Blueprint:
        doc = pv.load_blueprint(blueprint_id)
        blueprint = Blueprint(id=doc["blueprint_id"], outer_class=doc["outer_class"], class_order=list(doc["class_order"]))
        blueprint.validate()
        return blueprint


class _Profiles:
    async def get_agent_profile(self, agent_id: str) -> AgentProfile:
        doc = pv.load_profile(agent_id)
        tokens: Dict[str, ProfileToken] = {tid: ProfileToken.from_dict(tid, data) for tid, data in doc["tokens"].items()}
        return AgentProfile(blueprint_id=doc["blueprint_id"], tokens=tokens)

    async def get_override_tokens(self, owner_type, owner_id):
        return {}


def _assemble(agent_id: str, tokens: _Tokens) -> str:
    bio = Mock()
    bio.format.return_value = ""
    bio.format_directives.return_value = ""
    service = PromptAssemblyService(
        token_repo=tokens, blueprint_repo=_Blueprints(), profile_repo=_Profiles(), security_port=_AllowAll(),
        formatter=ContextFormatter(), bio_formatter=bio,
    )
    return asyncio.run(service.assemble(agent_type=agent_id, user_id=None, account_id=None))


def render_static(agent_id: str) -> str:
    """The static template production assembles for `agent_id` from the snapshot files."""
    return _assemble(agent_id, _Tokens()).split(PROMPT_CACHE_BOUNDARY)[0]


def rendered_token_ids(agent_id: str) -> List[str]:
    """The ids of the token files the assembler read for `agent_id`, sorted."""
    tokens = _Tokens()
    _assemble(agent_id, tokens)
    return sorted(tokens.read)
