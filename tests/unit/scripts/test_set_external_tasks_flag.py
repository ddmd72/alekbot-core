"""The operator script that switches the external task queue on or off for one user (EXTERNAL_TASK_QUEUE_RFC §5.10),
run over the real FirestoreUserRepository on the evaluating fake: a dry run writes nothing, `--apply` writes the one
field, an unknown user is refused, and only 8 characters of the id are printed."""
from types import SimpleNamespace

import pytest

from scripts.set_external_tasks_flag import set_flag
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.user import UserProfile
from tests.support.firestore_eval import EvalDb

USER = "usr-1234567890abcdef"


@pytest.fixture
async def world():
    db = EvalDb()
    env = SimpleNamespace(domain_users_collection="users", is_production=False)
    repo = FirestoreUserRepository(db, env, FirestoreAccountRepository(db, "accounts"))
    await repo.create_user(UserProfile(user_id=USER, account_id="acc-1", email="alex@example.com", display_name="Alex"))
    return repo, db


async def stored(db):
    return (await db.collection("users").document(USER).get()).to_dict()


async def run(repo, user_id=USER, **kw):
    lines = []
    code = await set_flag(repo, user_id, out=lines.append, **kw)
    return code, lines


async def test_a_dry_run_prints_current_and_target_and_writes_nothing(world):
    repo, db = world
    before = await stored(db)
    code, lines = await run(repo, enabled=True, apply=False)
    assert code == 0 and await stored(db) == before
    assert "is False, target True" in lines[0] and "nothing written" in lines[1]


async def test_apply_switches_it_on_and_changes_only_that_field(world):
    repo, db = world
    before = await stored(db)
    code, lines = await run(repo, enabled=True, apply=True)
    after = await stored(db)
    assert code == 0 and after["config"]["external_tasks_enabled"] is True
    assert {k: v for k, v in after.items() if k not in ("config", "updated_at")} == \
        {k: v for k, v in before.items() if k not in ("config", "updated_at")}
    assert {k: v for k, v in after["config"].items() if k != "external_tasks_enabled"} == \
        {k: v for k, v in before["config"].items() if k != "external_tasks_enabled"}
    assert any("within about an hour" in line for line in lines)


async def test_off_switches_it_back(world):
    repo, db = world
    await run(repo, enabled=True, apply=True)
    code, _ = await run(repo, enabled=False, apply=True)
    assert code == 0 and (await stored(db))["config"]["external_tasks_enabled"] is False


async def test_an_unknown_user_is_refused_and_nothing_is_created(world):
    repo, db = world
    code, lines = await run(repo, user_id="usr-ghost-0000000000", enabled=True, apply=True)
    assert code == 1 and "not found" in lines[0]
    assert (await db.collection("users").document("usr-ghost-0000000000").get()).exists is False


async def test_only_eight_characters_of_the_id_are_printed(world):
    repo, _ = world
    _, lines = await run(repo, enabled=True, apply=True)
    printed = "\n".join(lines)
    assert USER[:8] in printed and USER[:9] not in printed
