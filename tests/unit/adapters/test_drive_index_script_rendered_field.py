"""The record's `script_rendered` flag (USER_DRIVE_INDEXING_RFC §25.3, parser@2; added to §25.2.1's fields by the arbiter):
a script-rendered page gets a template profile, so the planner reads the flag from the record. Both repositories keep it;
its codec round trip and its default are in the every-field case (test_firestore_drive_index_repo_serialization.py)."""
from datetime import datetime, timezone

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, eval_transactions, make_eval_repo
from tests.unit.services.drive_index_fakes import U_SCOPE


async def test_both_repositories_keep_it(mock_env_config):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="html", script_rendered=True,
                           modified_at=datetime(2026, 10, 13, tzinfo=timezone.utc))
    with eval_transactions():
        for repo in (InMemoryDriveIndexRepository(), make_eval_repo(EvalDb(), mock_env_config)):
            await repo.upsert_records([rec])
            assert (await repo.get_records(U_SCOPE, ["i"]))["i"].script_rendered is True
