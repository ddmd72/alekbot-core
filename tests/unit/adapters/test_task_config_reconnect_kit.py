"""TaskConfigPort kit, the R1 parts (SECRETS_AT_REST_RFC §6.2, F2, D-P5): `reconnect_notified` round-trips through the
whole-document `save_config`, and `list_user_ids` lists every stored config, on `FirestoreTaskConfigRepository` over
the evaluating Firestore fake (index refusal ON)."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_task_config"


@pytest.fixture
def repo():
    return FirestoreTaskConfigRepository(EvalDb(indexes=INDEXES_FILE), SimpleNamespace(task_config_collection=COL))


def docs(repo):
    return repo.db.collection(COL).docs


async def test_the_flag_is_stored_and_read_back(repo):
    await repo.save_config("user-a", TaskUserConfig(primary_list_id="L1", reconnect_notified=True))
    assert docs(repo)["user-a"]["reconnect_notified"] is True
    got = await repo.get_config("user-a")
    assert (got.primary_list_id, got.reconnect_notified) == ("L1", True)


async def test_a_document_without_the_field_reads_unflagged(repo):
    docs(repo)["user-a"] = {"primary_list_id": "L1", "subscriptions": []}
    assert (await repo.get_config("user-a")).reconnect_notified is False


async def test_a_read_modify_save_keeps_the_flag_and_a_reset_config_drops_it(repo):
    await repo.save_config("user-a", TaskUserConfig(primary_list_id="L1", reconnect_notified=True))
    config = await repo.get_config("user-a")
    await repo.save_config("user-a", replace(config, subscriptions=[]))
    assert (await repo.get_config("user-a")).reconnect_notified is True
    await repo.save_config("user-a", TaskUserConfig())                      # the disconnect's reset (D-P5)
    got = await repo.get_config("user-a")
    assert (got.primary_list_id, got.reconnect_notified) == (None, False)


async def test_set_primary_list_id_if_absent_keeps_the_flag(repo):
    await repo.save_config("user-a", TaskUserConfig(reconnect_notified=True))
    assert await repo.set_primary_list_id_if_absent("user-a", "L1") == "L1"
    got = await repo.get_config("user-a")
    assert (got.primary_list_id, got.reconnect_notified) == ("L1", True)


async def test_list_user_ids_lists_every_stored_config_sorted(repo):
    assert await repo.list_user_ids() == []
    await repo.save_config("user-b", TaskUserConfig(primary_list_id="L2"))
    await repo.save_config("user-a", TaskUserConfig())
    assert await repo.list_user_ids() == ["user-a", "user-b"]


# ----------------------------------------------------------------------------- set_reconnect_notified (field write)

async def test_the_field_write_leaves_every_other_field_as_stored(repo):
    sub = TaskSubscriptionConfig(sub_id="s1", list_id="list-1", expires_at=datetime(2026, 11, 3, 8, 30))
    await repo.save_config("user-a", TaskUserConfig(primary_list_id="list-1", subscriptions=[sub]))
    before = dict(docs(repo)["user-a"])
    assert await repo.set_reconnect_notified("user-a", True) is True
    after = docs(repo)["user-a"]
    assert after["reconnect_notified"] is True
    assert {k: v for k, v in after.items() if k != "reconnect_notified"} == \
        {k: v for k, v in before.items() if k != "reconnect_notified"}
    assert await repo.set_reconnect_notified("user-a", False) is True
    got = await repo.get_config("user-a")
    assert (got.primary_list_id, [s.sub_id for s in got.subscriptions], got.reconnect_notified) == \
        ("list-1", ["s1"], False)


async def test_a_concurrent_save_of_other_fields_is_not_undone(repo):
    """The sweep reads the config, a setup saves new subscriptions, then the sweep writes its flag: the subscriptions
    the setup saved stay."""
    await repo.save_config("user-a", TaskUserConfig(primary_list_id="list-1"))
    read_by_sweep = await repo.get_config("user-a")
    new_sub = TaskSubscriptionConfig(sub_id="s2", list_id="list-1", expires_at=datetime(2026, 11, 3, 8, 30))
    await repo.save_config("user-a", replace(read_by_sweep, subscriptions=[new_sub]))   # the setup's save
    await repo.set_reconnect_notified("user-a", True)                                  # the sweep's write
    got = await repo.get_config("user-a")
    assert [s.sub_id for s in got.subscriptions] == ["s2"] and got.reconnect_notified is True


async def test_the_field_write_on_a_missing_config_writes_nothing(repo):
    assert await repo.set_reconnect_notified("user-a", True) is False
    assert docs(repo) == {}
    assert await repo.list_user_ids() == []
