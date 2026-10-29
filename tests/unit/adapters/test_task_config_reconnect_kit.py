"""TaskConfigPort kit, the R1 parts (SECRETS_AT_REST_RFC §6.2, F2, D-P5): `reconnect_notified` round-trips through the
whole-document `save_config`, and `list_user_ids` lists every stored config, on `FirestoreTaskConfigRepository` over
the evaluating Firestore fake (index refusal ON)."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.task import TaskUserConfig
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
