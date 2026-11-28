"""UserRepository kit (P11 · G2): the docstring of `src/ports/user_repository.py` checked against
`FirestoreUserRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw
stored documents and on what readers return.

P11-G2-4 (concurrent profile updates lose one) is pinned through two real callers in
`tests/unit/services/test_language_preference_service_store_pins.py`. Not pinned (data audit): a stored value the model
rejects, and the case of a stored email.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

import src.adapters.firestore_user_repo as user_module
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserProfile
from tests.support.clock_instants import pin_datetime

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USERS = "eval_users"
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={USERS: "development_users"})
    return FirestoreUserRepository(db, SimpleNamespace(domain_users_collection=USERS, is_production=False), None)


def docs(repo):
    return repo.db.collection(USERS).docs


def alex(**over) -> UserProfile:
    fields = dict(user_id="u1", account_id="a1", display_name="Alex Doe", email="alex@example.com",
                  created_at=T0, updated_at=T0)
    fields.update(over)
    return UserProfile(**fields)


def clara(**over) -> UserProfile:
    fields = dict(user_id="u2", account_id="a2", display_name="Clara Moreira", email="clara@example.com",
                  created_at=T0, updated_at=T0)
    fields.update(over)
    return UserProfile(**fields)


async def test_get_user_missing_is_none(repo):
    assert await repo.get_user("u9") is None


async def test_round_trip_create_then_get(repo):
    created = await repo.create_user(alex(platform_identities={"slack": "U1"}))
    assert docs(repo)["u1"] == alex(platform_identities={"slack": "U1"}).model_dump()
    assert await repo.get_user("u1") == created


async def test_create_replaces_an_existing_profile(repo):
    await repo.create_user(alex(external_user_id="google|sub-1"))
    await repo.create_user(alex(display_name="Alex"))
    assert docs(repo)["u1"]["external_user_id"] is None and docs(repo)["u1"]["display_name"] == "Alex"


async def test_empty_tier_complexity_overrides_are_dropped_on_read(repo):
    await repo.create_user(alex())
    config = dict(docs(repo)["u1"]["config"])        # a nested edit does not reach the real backend: assign the field
    config["complexity_settings_overrides"] = {
        "small_talk": {"tier": ""}, "deep_reasoning": {"tier": "performance"}, "info_search": "broken"}
    docs(repo)["u1"]["config"] = config
    got = await repo.get_user("u1")
    assert list(got.config.complexity_settings_overrides) == [TaskComplexity.DEEP_REASONING]
    assert got.config.complexity_settings_overrides[TaskComplexity.DEEP_REASONING].tier == PerformanceTier.PERFORMANCE


async def test_unknown_keys_are_ignored_on_read_and_dropped_by_update(repo):
    await repo.create_user(alex())
    docs(repo)["u1"]["nickname"] = "Bo"
    got = await repo.get_user("u1")
    assert got == alex()
    await repo.update_user(got)
    assert "nickname" not in docs(repo)["u1"]


async def test_lookup_by_platform_identity_uses_the_dotted_path(repo):
    await repo.create_user(alex())
    docs(repo)["u1"]["platform_identities"] = {"telegram": "777"}
    await repo.create_user(clara(platform_identities={"slack": "777"}))
    assert (await repo.get_user_by_platform_id("telegram", "777")).user_id == "u1"
    assert (await repo.get_user_by_platform_id("slack", "777")).user_id == "u2"
    assert await repo.get_user_by_platform_id("phone", "777") is None


async def test_lookup_by_email_is_an_exact_match(repo):
    await repo.create_user(alex())
    await repo.create_user(clara())
    assert (await repo.get_user_by_email("clara@example.com")).user_id == "u2"
    assert await repo.get_user_by_email("robin@example.net") is None


async def test_lookup_by_external_id(repo):
    await repo.create_user(alex(external_user_id="google|sub-1"))
    assert (await repo.get_user_by_external_id("google|sub-1")).user_id == "u1"
    assert await repo.get_user_by_external_id("google|sub-2") is None


async def test_link_writes_the_identity_and_updated_at(repo, monkeypatch):
    await repo.create_user(alex(platform_identities={"slack": "U1"}))
    later = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    pin_datetime(monkeypatch, user_module, later)
    user = await repo.link_platform_identity("u1", "telegram", "777")
    assert docs(repo)["u1"]["platform_identities"] == {"slack": "U1", "telegram": "777"}
    assert docs(repo)["u1"]["updated_at"] == later and user == await repo.get_user("u1")


async def test_link_refuses_an_identity_held_by_another_user_and_writes_nothing(repo):
    await repo.create_user(alex())
    await repo.create_user(clara(platform_identities={"telegram": "777"}))
    before = {k: dict(v) for k, v in docs(repo).items()}
    raised = None
    try:
        await repo.link_platform_identity("u1", "telegram", "777")
    except ValueError as exc:
        raised = exc
    assert raised is not None and docs(repo) == before


async def test_relinking_the_same_identity_to_the_same_user_succeeds(repo):
    await repo.create_user(alex(platform_identities={"telegram": "777"}))
    user = await repo.link_platform_identity("u1", "telegram", "777")
    assert user.platform_identities == {"telegram": "777"} == docs(repo)["u1"]["platform_identities"]


async def test_link_and_unlink_of_a_missing_user_raise_value_error(repo):
    raised = []
    for call in (repo.link_platform_identity("u9", "telegram", "777"), repo.remove_platform_id("u9", "slack")):
        try:
            await call
        except ValueError as exc:
            raised.append(exc)
    assert len(raised) == 2 and docs(repo) == {}


@pytest.mark.h1_only("reads the fake's write log (versions)")
async def test_unlink_removes_only_that_platform_and_absent_is_a_no_op(repo, monkeypatch):
    await repo.create_user(alex(platform_identities={"slack": "U1", "telegram": "777"}))
    later = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    pin_datetime(monkeypatch, user_module, later)
    user = await repo.unlink_platform_identity("u1", "slack")
    assert docs(repo)["u1"]["platform_identities"] == {"telegram": "777"} == user.platform_identities
    assert docs(repo)["u1"]["updated_at"] == later
    version = repo.db.collection(USERS).versions["u1"]
    assert (await repo.unlink_platform_identity("u1", "slack")).platform_identities == {"telegram": "777"}
    assert repo.db.collection(USERS).versions["u1"] == version          # nothing written


async def test_field_updates_from_two_stale_reads_keep_both_changes(repo, monkeypatch):
    await repo.create_user(alex())
    first, second = await repo.get_user("u1"), await repo.get_user("u1")   # both read before either writes
    later = datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc)
    pin_datetime(monkeypatch, user_module, later)
    first.config.timezone, first.display_name = "Europe/Berlin", "not written"
    await repo.update_user_fields(first, ("config.timezone",))
    second.auth_metadata = {"locale": "pt-PT"}
    returned = await repo.update_user_fields(second, ("auth_metadata",))
    stored = docs(repo)["u1"]
    assert (stored["config"]["timezone"], stored["auth_metadata"], stored["display_name"]) == \
        ("Europe/Berlin", {"locale": "pt-PT"}, "Alex Doe")
    assert stored["updated_at"] == later == returned.updated_at
    assert stored["config"]["location"] == alex().config.location          # the rest of config is untouched


async def test_field_update_of_an_unknown_path_raises_and_writes_nothing(repo):
    await repo.create_user(alex())
    before = dict(docs(repo)["u1"])
    with pytest.raises(ValueError, match="config.no_such_field"):
        await repo.update_user_fields(alex(), ("config.no_such_field",))
    assert docs(repo)["u1"] == before


async def test_field_update_of_a_missing_user_raises_not_found(repo):
    with pytest.raises(NotFound):
        await repo.update_user_fields(alex(), ("display_name",))
    assert "u1" not in docs(repo)


# ------------------------------------------------------------------------------- moved from the mock-SDK tests

async def test_lookup_by_external_id_returns_the_whole_stored_profile(repo):
    await repo.create_user(alex(external_user_id="google|sub-1"))
    assert await repo.get_user_by_external_id("google|sub-1") == alex(external_user_id="google|sub-1")


async def test_a_refused_link_says_already_linked_and_names_the_holder(repo):
    """The cabinet routes answer 409 when the message contains "already linked" (`user_cabinet_app.py`)."""
    await repo.create_user(alex())
    await repo.create_user(clara(platform_identities={"telegram": "777"}))
    with pytest.raises(ValueError, match="already linked to user u2"):
        await repo.link_platform_identity("u1", "telegram", "777")


@pytest.mark.h1_only("interfere_on_txn_read: a commit between the transaction's read and its write exists only on the fake")
async def test_link_reads_the_user_inside_its_transaction(repo):
    """A commit to the user after the link's read aborts the link, whose re-run keeps that commit's change."""
    await repo.create_user(alex(platform_identities={"slack": "U1"}))
    repo.db.interfere_on_txn_read(USERS, "u1", {"display_name": "Alex"}, after_read=True)
    user = await repo.link_platform_identity("u1", "telegram", "777")
    assert docs(repo)["u1"]["display_name"] == "Alex" == user.display_name
    assert docs(repo)["u1"]["platform_identities"] == {"slack": "U1", "telegram": "777"}


@pytest.mark.h1_only("interfere_on_txn_query: a commit after a transactional query exists only on the fake")
async def test_link_runs_its_conflict_query_inside_its_transaction(repo):
    """A commit to a user the conflict query returned aborts the link, whose re-run keeps that commit's change."""
    await repo.create_user(alex(platform_identities={"telegram": "777"}))
    repo.db.interfere_on_txn_query(USERS, "u1", {"display_name": "Alex"})
    user = await repo.link_platform_identity("u1", "telegram", "777")
    assert docs(repo)["u1"]["display_name"] == "Alex" == user.display_name
