"""`seed_agent_prompt` (tests/support/prompt_seed.py) stores a profile as Firestore holds it; the production reader assembles it.

The tests are sync: `render_static` runs its own event loop (`asyncio.run`)."""
import asyncio

import pytest

from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from tests.support.firestore_eval import EvalDb
from tests.support.prompt_render import render_static
from tests.support.prompt_seed import seed_agent_prompt


@pytest.mark.parametrize("profile_id", ["quick", "smart", "router"])
def test_a_seeded_profile_assembles_the_prompt_the_snapshot_renders(profile_id):
    db, env = EvalDb(), EnvironmentConfig()

    async def seed_and_assemble() -> str:
        await seed_agent_prompt(db, env, profile_id)
        service = build_prompt_assembly_service(db, env)[0]
        return await service.assemble(agent_type=profile_id, user_id="u1", account_id="a1")

    assembled = asyncio.run(seed_and_assemble())
    assert assembled.split(PROMPT_CACHE_BOUNDARY)[0] == render_static(profile_id)


def test_user_namespace_tokens_are_stored_in_the_user_collection():
    db, env = EvalDb(), EnvironmentConfig()
    asyncio.run(seed_agent_prompt(db, env, "quick"))
    system = db.collection(f"{env.domain_prompt_tokens_collection}_system").docs
    user = db.collection(f"{env.domain_prompt_tokens_collection}_user").docs
    for token_id in ("ARCHETYPE_INTELLECTUAL_SNIPER", "HUMOR_PRESET_RANEVSKAYA", "VIBE_BATTLE_WEARY", "VOICE_APHORISTIC"):
        assert token_id in user
    assert "COGNITIVE_PROCESS_QUICK" in system and "COGNITIVE_PROCESS_QUICK" not in user
