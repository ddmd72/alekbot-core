"""Seed one agent's prompt into a Firestore fake, as Firestore holds it (P13 card F, matrix card M1).

Writes the profile of `prompts_snapshot/profiles/<profile_id>.yaml`, its blueprint and every token it lists into the four
prompt collections the environment names, so the production reader (`build_prompt_assembly_service`) assembles that
agent's real prompt offline. No overrides are written; a missing token file raises `FileNotFoundError`."""
from typing import Any

from tests.support import prompt_vocabulary as pv


async def seed_agent_prompt(db: Any, env_config: Any, profile_id: str) -> None:
    """Write the profile `profile_id`, its blueprint and every token it lists into `db`. Plain sets: idempotent."""
    profile = pv.load_profile(profile_id)
    await db.collection(env_config.domain_prompt_profiles_collection).document(profile_id).set(profile)

    blueprint = pv.load_blueprint(profile["blueprint_id"])
    await db.collection(env_config.domain_prompt_blueprints_collection).document(blueprint["blueprint_id"]).set(blueprint)

    for token_id in profile["tokens"]:
        path, namespace = pv.token_path(token_id), "system"
        if not path.exists():  # the "user"-namespace tokens, same rule as tests/support/prompt_render.py
            path, namespace = pv.SNAPSHOT / "tokens" / "user" / f"{token_id}.groovy", "user"
        doc = pv.ser.token_from_file(path.read_text(encoding="utf-8"))
        collection = f"{env_config.domain_prompt_tokens_collection}_{namespace}"
        await db.collection(collection).document(token_id).set(doc)
