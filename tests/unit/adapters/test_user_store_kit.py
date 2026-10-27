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

import src.adapters.firestore_user_repo as user_module
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserProfile
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USERS = "eval_users"
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={USERS: "development_users"})
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
    docs(repo)["u1"]["config"]["complexity_settings_overrides"] = {
        "small_talk": {"tier": ""}, "deep_reasoning": {"tier": "performance"}, "info_search": "broken"}
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


async def test_update_user_replaces_the_document_and_sets_updated_at(repo, monkeypatch):
    await repo.create_user(alex(external_user_id="google|sub-1"))
    later = datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc)
    pin_datetime(monkeypatch, user_module, later)
    changed = alex(display_name="Alex D.")
    returned = await repo.update_user(changed)
    assert returned.updated_at == later
    assert docs(repo)["u1"] == alex(display_name="Alex D.", updated_at=later).model_dump()   # external id gone


async def test_serial_updates_keep_both_changes(repo):
    await repo.create_user(alex())
    first = await repo.get_user("u1")
    first.config.timezone = "Europe/Berlin"
    await repo.update_user(first)
    second = await repo.get_user("u1")
    second.config.location = "Springfield"
    await repo.update_user(second)
    stored = docs(repo)["u1"]["config"]
    assert (stored["timezone"], stored["location"]) == ("Europe/Berlin", "Springfield")
