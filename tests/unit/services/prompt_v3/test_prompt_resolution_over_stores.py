"""The 4-level prompt resolution (USER > ACCOUNT > AGENT profile > SYSTEM tokens) through the REAL `PromptAssemblyService`
over the three real v3 stores on one `EvalDb` (P10). The service is built by `build_prompt_assembly_service`, the function
`ServiceContainer` and the indexer call, with a real `EnvironmentConfig`; assertions are on the assembled prompt text.

Seeding: overrides go through `set_override_tokens`. Tokens, blueprints and agent profiles are seeded as raw documents,
because `FirestoreTokenRepository.save` and `FirestoreBlueprintRepository.save` do not write on the production client
(P10-D1, P10-D3, pinned in the kit files) and the profile port has no writer.

Not repeated here: the cache key, TTL and `invalidate_cache` mechanics (`test_prompt_assembly_cache.py`) and the runtime
blocks (`test_prompt_assembly_service.py`); this file covers what a write does to the next assembly over real stores.
"""
import pytest

from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from tests.support.firestore_eval import EvalDb

BASE = "Answer in two short sentences."
ACCOUNT_STYLE = "Use a formal register."
USER_STYLE = "Answer in one word."
OTHER_STYLE = "Answer with a bulleted list."
METRIC = "Use metric units."
VOICE = "Speak in the present tense."


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.env = EnvironmentConfig()
        self.service, self.profiles = build_prompt_assembly_service(self.db, self.env)
        self.tokens = self.service.token_repo

    def _docs(self, name: str) -> dict:
        return self.db.collection(name).docs

    def token(self, token_id, content, category="style", class_="properties", where="system") -> None:
        collection = f"{self.env.domain_prompt_tokens_collection}_{where}"
        self._docs(collection)[token_id] = {"token_id": token_id, "category": category, "class": class_,
                                            "content": content, "metadata": {}}

    def blueprint(self, blueprint_id="bp_one", class_order=("properties", "policies")) -> None:
        self._docs(self.env.domain_prompt_blueprints_collection)[blueprint_id] = {
            "blueprint_id": blueprint_id, "outer_class": "Alek extends Agent", "class_order": list(class_order)}

    def profile(self, agent="smart", blueprint_id="bp_one", **tokens) -> None:
        """`tokens`: token id -> (order, non_overridable)."""
        self._docs(self.env.domain_prompt_profiles_collection)[agent] = {
            "blueprint_id": blueprint_id, "agent_id": agent,
            "tokens": {tid: {"order": o, **({"non_overridable": True} if locked else {})} for tid, (o, locked) in tokens.items()}}

    async def override(self, owner_type, owner_id, **tokens) -> None:
        await self.profiles.set_override_tokens(
            owner_type, owner_id, {tid: ProfileToken(token_id=tid, order=o) for tid, o in tokens.items()})

    async def prompt(self, user="usr-1", account="acc-1", agent="smart") -> str:
        return await self.service.assemble(agent_type=agent, user_id=user, account_id=account)


@pytest.fixture
def world() -> World:
    w = World()
    w.blueprint()
    w.token("STYLE_BASE", BASE)
    w.token("STYLE_ACCOUNT", ACCOUNT_STYLE)
    w.token("STYLE_USER", USER_STYLE)
    w.token("UNITS_METRIC", METRIC, category="units", class_="policies")
    w.profile(STYLE_BASE=(10, False))
    return w


# ------------------------------------------------------------------------------------- system and profile

async def test_a_profile_with_one_system_token_renders_that_token_in_the_blueprints_outer_class(world):
    prompt = await world.prompt()
    assert "class Alek extends Agent {" in prompt
    assert BASE in prompt and "properties {" in prompt
    assert ACCOUNT_STYLE not in prompt and USER_STYLE not in prompt and METRIC not in prompt


async def test_the_profile_selects_which_system_tokens_an_agent_gets(world):
    world.token("STYLE_OTHER", OTHER_STYLE)
    world.profile("quick", STYLE_OTHER=(10, False))
    assert OTHER_STYLE in await world.prompt(agent="quick") and BASE not in await world.prompt(agent="quick")
    assert BASE in await world.prompt(agent="smart") and OTHER_STYLE not in await world.prompt(agent="smart")


async def test_sections_follow_the_blueprint_class_order_and_tokens_the_profile_order(world):
    world.token("STYLE_SECOND", "Prefer plain words.")
    world.profile(UNITS_METRIC=(10, False), STYLE_SECOND=(30, False), STYLE_BASE=(20, False))
    prompt = await world.prompt()
    assert prompt.index("properties {") < prompt.index("policies {")
    assert prompt.index(BASE) < prompt.index("Prefer plain words.") < prompt.index(METRIC)


async def test_a_profile_token_missing_from_the_token_store_is_skipped(world):
    world.profile(STYLE_BASE=(10, False), GHOST=(20, False))
    prompt = await world.prompt()
    assert BASE in prompt and "GHOST" not in prompt


async def test_a_token_in_the_user_token_collection_is_found_when_the_system_one_lacks_it(world):
    world.token("STYLE_BASE", "Answer from the user collection.", where="user")
    world._docs(f"{world.env.domain_prompt_tokens_collection}_system").pop("STYLE_BASE")
    assert "Answer from the user collection." in await world.prompt()


async def test_a_profile_without_tokens_renders_the_empty_outer_class(world):
    world.profile()
    assert "class Alek extends Agent {" in await world.prompt() and BASE not in await world.prompt()


async def test_an_agent_without_a_profile_needs_its_derived_blueprint(world):
    with pytest.raises(KeyError):
        await world.prompt(agent="router")
    world.blueprint("router_agent_v1")
    assert "class Alek extends Agent {" in await world.prompt(agent="router")


# --------------------------------------------------------------------------------------------- the 4 levels

async def test_an_account_override_wins_over_the_profile(world):
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    prompt = await world.prompt()
    assert ACCOUNT_STYLE in prompt and BASE not in prompt


async def test_a_user_override_wins_over_the_account_override(world):
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    prompt = await world.prompt()
    assert USER_STYLE in prompt and ACCOUNT_STYLE not in prompt and BASE not in prompt


async def test_removing_the_user_override_falls_back_to_the_account_then_to_the_profile(world):
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    assert USER_STYLE in await world.prompt()

    await world.profiles.delete_profile(OwnerType.USER, "usr-1")
    world.service.invalidate_cache()
    prompt = await world.prompt()
    assert ACCOUNT_STYLE in prompt and USER_STYLE not in prompt

    await world.profiles.delete_profile(OwnerType.ACCOUNT, "acc-1")
    world.service.invalidate_cache()
    prompt = await world.prompt()
    assert BASE in prompt and ACCOUNT_STYLE not in prompt


async def test_an_override_replaces_only_the_token_of_its_own_class_and_category(world):
    world.profile(STYLE_BASE=(10, False), UNITS_METRIC=(20, False))
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    prompt = await world.prompt()
    assert USER_STYLE in prompt and METRIC in prompt and BASE not in prompt


async def test_an_override_from_the_user_token_collection_applies(world):
    world.token("STYLE_USER", USER_STYLE, where="user")
    world._docs(f"{world.env.domain_prompt_tokens_collection}_system").pop("STYLE_USER")
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    assert USER_STYLE in await world.prompt()


async def test_the_override_keeps_its_own_order_among_the_sections_tokens(world):
    world.token("STYLE_SECOND", "Prefer plain words.", category="plain")
    world.profile(STYLE_BASE=(10, False), STYLE_SECOND=(20, False))
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=30)
    prompt = await world.prompt()
    assert prompt.index("Prefer plain words.") < prompt.index(USER_STYLE)


# --------------------------------------------------------------------------------------------- the edge cases

async def test_an_override_naming_a_token_that_does_not_exist_is_ignored(world):
    await world.override(OwnerType.USER, "usr-1", STYLE_NOWHERE=10)
    prompt = await world.prompt()
    assert BASE in prompt and "STYLE_NOWHERE" not in prompt


async def test_an_override_for_a_class_and_category_the_profile_does_not_use_is_ignored(world):
    world.token("VOICE_PRESENT", VOICE, category="voice", class_="policies")
    await world.override(OwnerType.USER, "usr-1", VOICE_PRESENT=10)
    prompt = await world.prompt()
    assert VOICE not in prompt and BASE in prompt


async def test_a_non_overridable_profile_token_blocks_account_and_user_overrides(world):
    world.profile(STYLE_BASE=(10, True))
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    prompt = await world.prompt()
    assert BASE in prompt and ACCOUNT_STYLE not in prompt and USER_STYLE not in prompt


async def test_a_user_override_still_applies_after_an_account_override_replaced_the_profile_token(world):
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    prompt = await world.prompt()
    assert USER_STYLE in prompt and ACCOUNT_STYLE not in prompt


# ----------------------------------------------------------------------------------------------- tenancy

async def test_one_users_override_never_reaches_another_user_of_the_same_account(world):
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    assert USER_STYLE in await world.prompt(user="usr-1")
    other = await world.prompt(user="usr-2")
    assert USER_STYLE not in other and BASE in other


async def test_one_accounts_override_never_reaches_another_account(world):
    await world.override(OwnerType.ACCOUNT, "acc-1", STYLE_ACCOUNT=10)
    assert ACCOUNT_STYLE in await world.prompt(account="acc-1")
    other = await world.prompt(account="acc-2")
    assert ACCOUNT_STYLE not in other and BASE in other


async def test_a_user_id_equal_to_an_account_id_does_not_cross_levels(world):
    await world.override(OwnerType.USER, "same-1", STYLE_USER=10)
    await world.override(OwnerType.ACCOUNT, "same-1", STYLE_ACCOUNT=10)
    as_account = await world.prompt(user="usr-2", account="same-1")
    assert ACCOUNT_STYLE in as_account and USER_STYLE not in as_account
    as_user = await world.prompt(user="same-1", account="acc-2")
    assert USER_STYLE in as_user and ACCOUNT_STYLE not in as_user


async def test_without_user_and_account_ids_only_the_profile_applies(world):
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    prompt = await world.service.assemble(agent_type="smart", user_id=None, account_id=None)
    assert BASE in prompt and USER_STYLE not in prompt


# ------------------------------------------------------------------------------- writes after an assembly

async def test_a_write_after_an_assembly_is_not_visible_to_the_same_user_until_the_cache_is_cleared(world):
    assert BASE in await world.prompt()
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    assert BASE in await world.prompt() and USER_STYLE not in await world.prompt()
    world.service.invalidate_cache()
    assert USER_STYLE in await world.prompt()


async def test_a_write_after_an_assembly_is_visible_at_once_to_a_user_who_has_no_cached_prompt(world):
    assert BASE in await world.prompt(user="usr-1")
    await world.override(OwnerType.USER, "usr-2", STYLE_USER=10)
    assert USER_STYLE in await world.prompt(user="usr-2")


async def test_a_write_after_an_assembly_is_visible_once_the_cached_entry_is_older_than_the_ttl(world):
    await world.prompt()
    await world.override(OwnerType.USER, "usr-1", STYLE_USER=10)
    key = world.service._build_cache_key("smart", "acc-1", "usr-1")
    text, stamp = world.service._assembled_cache[key]
    world.service._assembled_cache[key] = (text, stamp - world.service._cache_ttl - 1)
    assert USER_STYLE in await world.prompt()


async def test_a_change_to_a_token_text_is_not_visible_until_the_cache_is_cleared(world):
    assert BASE in await world.prompt()
    world.token("STYLE_BASE", "Answer in three short sentences.")
    assert BASE in await world.prompt()
    world.service.invalidate_cache()
    assert "Answer in three short sentences." in await world.prompt()
