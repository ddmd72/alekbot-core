"""G3 can run the branch's prompt files and pick the specialist's provider (scripts/validation/drive_eval/g3_overrides.py):
the overlay serves exactly the six snapshot files and every other id from the real repositories, a run without the flag
wraps nothing, the provider and tier override changes a copy only, and the report says which setup ran."""

import pytest

from scripts.consolidation import s3_dry_run_kit as kit
from scripts.validation.drive_eval import g3_overrides as g3
from scripts.validation.drive_eval.smart_dry_run import _args
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.user import PerformanceTier, UserBotConfig
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

SNAP = kit.SNAPSHOT_DIR


class AllowAll(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(sanitized_text=text, risk_level=RiskLevel.SAFE, risk_score=0.0, patterns_detected=[],
                                action_taken="passed", metadata={})


def firestore_token(token_id: str) -> Token:
    return Token(id=TokenId(token_id), category=TokenCategory("policy_set"), class_=TokenClass("policies"),
                 content="from firestore", metadata={})


class Tokens(TokenRepository):
    def __init__(self, *ids: str) -> None:
        self.tokens = {i: firestore_token(i) for i in ids}

    async def get(self, token_id):
        return self.tokens[str(token_id)]

    async def list_by_category(self, category):
        return [t for t in self.tokens.values() if t.category == category]

    async def list_by_class(self, token_class):
        return [t for t in self.tokens.values() if t.class_ == token_class]

    async def list_all(self):
        return list(self.tokens.values())

    async def save(self, token):
        self.tokens[str(token.id)] = token

    async def delete(self, token_id):
        del self.tokens[str(token_id)]

    async def exists(self, token_id):
        return str(token_id) in self.tokens


class Blueprints(BlueprintRepository):
    def __init__(self) -> None:
        self.blueprints = {"other_v1": Blueprint(id="other_v1", outer_class="Other extends Agent", class_order=["policies"])}

    async def get(self, blueprint_id):
        return self.blueprints[blueprint_id]

    async def list_all(self):
        return list(self.blueprints.values())

    async def exists(self, blueprint_id):
        return blueprint_id in self.blueprints

    async def save(self, blueprint):
        self.blueprints[blueprint.id] = blueprint

    async def delete(self, blueprint_id):
        del self.blueprints[blueprint_id]


class Profiles(AgentProfileRepository):
    async def get_agent_profile(self, agent_id):
        return AgentProfile(blueprint_id=f"{agent_id}_firestore", tokens={})

    async def get_override_tokens(self, owner_type, owner_id):
        return {}

    async def set_override_tokens(self, owner_type, owner_id, tokens, clear_ids=None):
        raise AssertionError("a live profile write")

    async def delete_profile(self, owner_type, owner_value):
        raise AssertionError("a live profile delete")


def service() -> PromptAssemblyService:
    live = Tokens(*g3.OVERLAY_TOKEN_IDS, "PROTOCOL_QUICK_AGENT_SELECTION")
    return PromptAssemblyService(live, Blueprints(), Profiles(), AllowAll(), None, None)   # type: ignore[arg-type]


def test_every_overlay_id_is_a_file_of_the_snapshot_and_there_are_six():
    files = g3.overlay_files(SNAP)
    assert len(files) == 6 and all(f.is_file() for f in files)
    assert {f.stem for f in files} == {*g3.OVERLAY_TOKEN_IDS, "drive_agent_v1", "drive"}


async def test_the_overlay_serves_the_six_ids_from_the_snapshot_and_every_other_id_from_the_real_repositories():
    svc = service()
    svc._assembled_cache["warm"] = "stale prompt"   # type: ignore[attr-defined]

    g3.install_snapshot_prompts(svc, SNAP)

    for token_id in g3.OVERLAY_TOKEN_IDS:
        served = await svc.token_repo.get(TokenId(token_id))
        body = (SNAP / "tokens" / "system" / f"{token_id}.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]
        assert served.content == body != "from firestore"
    assert (await svc.token_repo.get(TokenId("PROTOCOL_QUICK_AGENT_SELECTION"))).content == "from firestore"
    drive = await svc.profile_repo.get_agent_profile("drive")
    assert drive.blueprint_id == "drive_agent_v1" and sorted(drive.tokens) == [
        "COGNITIVE_PROCESS_DRIVE", "OUTPUT_FORMAT_DRIVE", "PROTOCOL_DRIVE_SEARCH"]
    assert (await svc.profile_repo.get_agent_profile("smart")).blueprint_id == "smart_firestore"
    assert (await svc.blueprint_repo.get("drive_agent_v1")).id == "drive_agent_v1"
    assert (await svc.blueprint_repo.get("other_v1")).id == "other_v1"
    assert svc._assembled_cache == {}   # type: ignore[attr-defined]
    with pytest.raises(PermissionError):
        await svc.token_repo.save(firestore_token("X"))
    with pytest.raises(PermissionError):
        await svc.blueprint_repo.delete("drive_agent_v1")


async def test_the_real_assembly_renders_the_drive_specialists_prompt_from_the_snapshot_only():
    svc = service()
    g3.install_snapshot_prompts(svc, SNAP)

    prompt = await svc._assemble_static_template("drive", "acc", "usr")

    assert "from firestore" not in prompt
    for token_id in ("COGNITIVE_PROCESS_DRIVE", "PROTOCOL_DRIVE_SEARCH", "OUTPUT_FORMAT_DRIVE"):
        text = (SNAP / "tokens" / "system" / f"{token_id}.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]
        longest = max((line.strip() for line in text.splitlines()), key=len)
        assert longest in prompt


def test_a_snapshot_missing_one_of_the_files_fails_before_any_agent_is_built(tmp_path):
    svc = service()
    live = (svc.token_repo, svc.profile_repo, svc.blueprint_repo)

    with pytest.raises(FileNotFoundError):
        g3.install_snapshot_prompts(svc, tmp_path)

    assert (svc.token_repo, svc.profile_repo, svc.blueprint_repo) == live   # nothing was wrapped


def test_without_the_flag_nothing_is_wrapped_and_the_setup_says_firestore():
    args = _args(["--max-usd", "1"])
    svc = service()
    live = (svc.token_repo, svc.profile_repo, svc.blueprint_repo)

    assert args.prompts_from_snapshot is False   # the run installs the overlay only when this is on
    assert (svc.token_repo, svc.profile_repo, svc.blueprint_repo) == live
    setup = g3.run_setup(g3.PROMPTS_FIRESTORE, args.specialist_provider, args.specialist_tier)
    assert setup == {"prompts": "firestore", "specialist_provider": "config", "specialist_tier": "config"}


def test_the_new_flags_parse_and_reject_other_values(capsys):
    args = _args(["--max-usd", "2", "--prompts-from-snapshot", "--specialist-provider", "openai",
                  "--specialist-tier", "performance"])
    assert (args.prompts_from_snapshot, args.specialist_provider, args.specialist_tier) == (True, "openai", "performance")
    for bad in (["--specialist-provider", "gemini"], ["--specialist-tier", "ultra"]):
        with pytest.raises(SystemExit):
            _args(["--max-usd", "1", *bad])
    capsys.readouterr()


def test_the_override_changes_a_copy_and_leaves_the_stored_config_alone():
    stored = UserBotConfig(agent_providers={"smart": "openai"}, agent_tiers={"smart": PerformanceTier.BALANCED})
    before = stored.model_dump()

    changed = g3.specialist_config(stored, "claude", "balanced")

    assert changed is not stored and stored.model_dump() == before
    assert changed.get_provider_for_agent("drive") == "claude"
    assert changed.get_tier_for_agent("drive") == PerformanceTier.BALANCED
    assert changed.get_provider_for_agent("smart") == "openai"   # the other agents keep their stored choice
    assert changed.get_tier_for_agent("smart") == PerformanceTier.BALANCED


def test_no_override_hands_back_the_same_config_and_one_override_leaves_the_other_field_alone():
    stored = UserBotConfig(agent_providers={"drive": "openai"})
    assert g3.specialist_config(stored, None, None) is stored
    tier_only = g3.specialist_config(stored, None, "performance")
    assert tier_only.get_provider_for_agent("drive") == "openai"
    assert tier_only.get_tier_for_agent("drive") == PerformanceTier.PERFORMANCE
    assert g3.specialist_config(UserBotConfig(), "openai", None).get_tier_for_agent("drive") == (
        UserBotConfig().get_tier_for_agent("drive"))


def test_the_setup_line_names_prompts_provider_and_tier_so_a_before_after_pair_is_never_confused():
    after = g3.run_setup(g3.PROMPTS_SNAPSHOT, "openai", "performance")
    before = g3.run_setup(g3.PROMPTS_FIRESTORE, None, None)
    assert g3.setup_line(after) == "prompts: snapshot | specialist provider: openai, tier: performance"
    assert g3.setup_line(before) == "prompts: firestore | specialist provider: config, tier: config"
    assert after != before
