"""FirestoreAgentProfileRepository kit (P10): the docstring of `src/ports/prompt_v3/agent_profile_repository.py` and of the
adapter checked on the evaluating Firestore fake (H1). Profiles live in one collection keyed by agent id; ACCOUNT and USER
overrides share a second one keyed `{OWNER_TYPE}_{owner_id}`. The port has no profile writer, so profiles are seeded as raw
documents; overrides are written through `set_override_tokens`.

`_adapter(db)` is the one place the adapter is constructed.
"""
import pytest

from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from tests.support.firestore_eval import EvalDb

PROF, OVR = "eval_profiles", "eval_overrides"


def _adapter(db: EvalDb) -> FirestoreAgentProfileRepository:
    return FirestoreAgentProfileRepository(db=db, profiles_collection=PROF, overrides_collection=OVR)


class Store:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.repo = _adapter(self.db)

    def docs(self, collection: str) -> dict:
        return self.db.collection(collection).docs


@pytest.fixture
def store() -> Store:
    return Store()


def pt(token_id, order=10, locked=False) -> ProfileToken:
    return ProfileToken(token_id=token_id, order=order, non_overridable=locked)


# --------------------------------------------------------------------------------------------- agent profile

async def test_a_profile_is_read_with_its_blueprint_and_every_token_field(store):
    store.docs(PROF)["quick"] = {"blueprint_id": "universal_agent_v1", "agent_id": "quick", "tokens": {
        "COGNITIVE_QUICK": {"order": 10, "non_overridable": True}, "STYLE_SHORT": {"order": 40}}}
    profile = await store.repo.get_agent_profile("quick")
    assert profile.blueprint_id == "universal_agent_v1"
    assert profile.tokens == {"COGNITIVE_QUICK": pt("COGNITIVE_QUICK", 10, True), "STYLE_SHORT": pt("STYLE_SHORT", 40, False)}


async def test_a_missing_profile_is_empty_with_a_derived_blueprint_id(store):
    profile = await store.repo.get_agent_profile("router")
    assert (profile.blueprint_id, profile.tokens) == ("router_agent_v1", {})


async def test_a_profile_without_blueprint_id_derives_it(store):
    store.docs(PROF)["smart"] = {"tokens": {"A": {"order": 1}}}
    profile = await store.repo.get_agent_profile("smart")
    assert profile.blueprint_id == "smart_agent_v1" and list(profile.tokens) == ["A"]


async def test_a_malformed_token_entry_is_skipped(store):
    store.docs(PROF)["quick"] = {"blueprint_id": "b", "tokens": {"GOOD": {"order": 1}, "BAD": "text"}}
    assert list((await store.repo.get_agent_profile("quick")).tokens) == ["GOOD"]


async def test_profiles_are_per_agent(store):
    store.docs(PROF)["quick"] = {"blueprint_id": "bq", "tokens": {"A": {"order": 1}}}
    store.docs(PROF)["smart"] = {"blueprint_id": "bs", "tokens": {"B": {"order": 2}}}
    assert list((await store.repo.get_agent_profile("smart")).tokens) == ["B"]


# ------------------------------------------------------------------------------------------------ overrides

async def test_overrides_of_nobody_are_empty(store):
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-1") == {}


async def test_set_then_get_round_trips_every_field(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"A": pt("A", 10, True), "B": pt("B", 20)})
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-1") == {"A": pt("A", 10, True), "B": pt("B", 20)}


async def test_the_override_document_is_keyed_by_owner_type_and_id(store):
    await store.repo.set_override_tokens(OwnerType.ACCOUNT, "acc-1", {"A": pt("A", 10)})
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"B": pt("B", 20)})
    assert sorted(store.docs(OVR)) == ["ACCOUNT_acc-1", "USER_usr-1"]
    assert store.docs(OVR)["USER_usr-1"]["owner_type"] == "USER" and store.docs(OVR)["USER_usr-1"]["owner_id"] == "usr-1"
    assert store.docs(PROF) == {}


async def test_user_and_account_overrides_never_mix_even_with_the_same_id(store):
    await store.repo.set_override_tokens(OwnerType.USER, "same-1", {"U": pt("U")})
    await store.repo.set_override_tokens(OwnerType.ACCOUNT, "same-1", {"A": pt("A")})
    assert list(await store.repo.get_override_tokens(OwnerType.USER, "same-1")) == ["U"]
    assert list(await store.repo.get_override_tokens(OwnerType.ACCOUNT, "same-1")) == ["A"]


async def test_one_users_overrides_are_invisible_to_another(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"A": pt("A")})
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-2") == {}


async def test_one_accounts_overrides_are_invisible_to_another(store):
    await store.repo.set_override_tokens(OwnerType.ACCOUNT, "acc-1", {"A": pt("A")})
    assert await store.repo.get_override_tokens(OwnerType.ACCOUNT, "acc-2") == {}


async def test_set_upserts_and_keeps_the_tokens_it_does_not_name(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"A": pt("A", 10), "B": pt("B", 20)})
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"B": pt("B", 25, True), "C": pt("C", 30)})
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-1") == {
        "A": pt("A", 10), "B": pt("B", 25, True), "C": pt("C", 30)}


async def test_clear_ids_remove_tokens_in_the_same_write(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"LANG_EN": pt("LANG_EN", 10), "B": pt("B", 20)})
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"LANG_FR": pt("LANG_FR", 10)}, clear_ids={"LANG_EN"})
    assert sorted(await store.repo.get_override_tokens(OwnerType.USER, "usr-1")) == ["B", "LANG_FR"]


async def test_a_clear_id_that_is_not_stored_is_ignored(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"A": pt("A")})
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {}, clear_ids={"NOPE"})
    assert list(await store.repo.get_override_tokens(OwnerType.USER, "usr-1")) == ["A"]


async def test_an_empty_set_on_a_missing_document_creates_an_empty_override(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {})
    assert store.docs(OVR)["USER_usr-1"]["tokens"] == {}
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-1") == {}


async def test_a_malformed_override_entry_is_skipped(store):
    store.docs(OVR)["USER_usr-1"] = {"owner_type": "USER", "owner_id": "usr-1", "tokens": {"GOOD": {"order": 1}, "BAD": 5}}
    assert list(await store.repo.get_override_tokens(OwnerType.USER, "usr-1")) == ["GOOD"]


# --------------------------------------------------------------------------------------------- delete_profile

async def test_delete_profile_of_an_agent_removes_it_from_the_profiles_collection(store):
    store.docs(PROF)["quick"] = {"blueprint_id": "b", "tokens": {}}
    store.docs(PROF)["smart"] = {"blueprint_id": "b", "tokens": {}}
    await store.repo.delete_profile(OwnerType.AGENT, "quick")
    assert list(store.docs(PROF)) == ["smart"]


async def test_delete_profile_of_a_user_removes_only_that_users_overrides(store):
    await store.repo.set_override_tokens(OwnerType.USER, "usr-1", {"A": pt("A")})
    await store.repo.set_override_tokens(OwnerType.USER, "usr-2", {"B": pt("B")})
    await store.repo.set_override_tokens(OwnerType.ACCOUNT, "usr-1", {"C": pt("C")})
    await store.repo.delete_profile(OwnerType.USER, "usr-1")
    assert sorted(store.docs(OVR)) == ["ACCOUNT_usr-1", "USER_usr-2"]
    assert await store.repo.get_override_tokens(OwnerType.USER, "usr-1") == {}


async def test_delete_profile_of_an_account_removes_the_account_document(store):
    await store.repo.set_override_tokens(OwnerType.ACCOUNT, "acc-1", {"A": pt("A")})
    await store.repo.delete_profile(OwnerType.ACCOUNT, "acc-1")
    assert store.docs(OVR) == {}


async def test_delete_profile_of_a_missing_document_is_a_no_op(store):
    store.docs(PROF)["quick"] = {"blueprint_id": "b", "tokens": {}}
    await store.repo.delete_profile(OwnerType.USER, "usr-9")
    assert list(store.docs(PROF)) == ["quick"] and store.docs(OVR) == {}
