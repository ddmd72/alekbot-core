"""The drive specialist's prompt assembles from its snapshot files (DRIVE_SPECIALIST_RFC §5.2): profile `drive`,
blueprint `drive_agent_v1`, the three tokens in class order, a cache boundary for the state block to follow."""
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from firestore_utils import snapshot_serializer as serializer
from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

_SNAPSHOT = Path(__file__).resolve().parents[4] / "prompts_snapshot"


class _AllowAll(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(is_safe=True, risk_level=RiskLevel.LOW, detected_patterns=[], sanitized_text=text)


def _token(name):
    doc = serializer.token_from_file((_SNAPSHOT / "tokens" / "system" / f"{name}.groovy").read_text())
    return Token(id=TokenId(doc["token_id"]), category=TokenCategory(doc["category"]),
                 class_=TokenClass(doc["class"]), content=doc["content"], metadata=doc.get("metadata", {}))


def _service():
    profile_doc = serializer.doc_from_yaml((_SNAPSHOT / "profiles" / "drive.yaml").read_text())
    blueprint_doc = serializer.doc_from_yaml((_SNAPSHOT / "blueprints" / "drive_agent_v1.yaml").read_text())
    profile = AgentProfile(blueprint_id=profile_doc["blueprint_id"], tokens={
        tid: ProfileToken(token_id=tid, order=spec["order"], non_overridable=spec["non_overridable"])
        for tid, spec in profile_doc["tokens"].items()})
    tokens = {TokenId(n): _token(n) for n in profile_doc["tokens"]}
    token_repo, blueprint_repo, profile_repo = AsyncMock(), AsyncMock(), AsyncMock()
    token_repo.get = AsyncMock(side_effect=lambda tid: tokens[tid])
    blueprint_repo.get = AsyncMock(return_value=Blueprint(
        id=blueprint_doc["blueprint_id"], outer_class=blueprint_doc["outer_class"],
        class_order=blueprint_doc["class_order"]))
    profile_repo.get_agent_profile = AsyncMock(return_value=profile)
    profile_repo.get_override_tokens = AsyncMock(return_value={})
    bio = Mock()
    bio.format.return_value = ""
    return PromptAssemblyService(token_repo=token_repo, blueprint_repo=blueprint_repo, profile_repo=profile_repo,
                                 security_port=_AllowAll(), formatter=ContextFormatter(), bio_formatter=bio)


def _first_line(name):
    """A token body's first line, read from its snapshot file: a position mark that follows any rewording."""
    return _token(name).content.strip().splitlines()[0].strip()


async def test_the_drive_prompt_assembles_in_class_order():
    prompt = await _service().assemble(agent_type="drive", user_id="u1", account_id=None)
    static = prompt.split(PROMPT_CACHE_BOUNDARY)[0]
    assert static.lstrip().startswith("class DriveAgent extends Agent {")
    for cls in ("cognitive_process", "protocols", "output_format"):   # a token body is not wrapped in its class twice
        assert f"{cls} {{\n\n        {cls}" not in static
    marks = ["    cognitive_process {", _first_line("COGNITIVE_PROCESS_DRIVE"),
             "    protocols {", _first_line("PROTOCOL_DRIVE_SEARCH"),
             "    output_format {", _first_line("OUTPUT_FORMAT_DRIVE")]
    positions = [static.index(m) for m in marks]
    assert positions == sorted(positions)
    assert PROMPT_CACHE_BOUNDARY in prompt
