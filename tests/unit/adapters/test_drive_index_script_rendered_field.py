"""The record's `script_rendered` flag (USER_DRIVE_INDEXING_RFC §25.3, parser@2; added to §25.2.1's fields by the arbiter):
a script-rendered page gets a template profile, so the planner reads the flag from the record. It round-trips through
the Firestore codec and both repositories, and a record stored before it reads as False."""
from datetime import datetime, timezone

from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, eval_transactions, make_eval_repo
from tests.unit.services.drive_index_fakes import U_SCOPE


def test_a_stored_field_that_round_trips():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="html", script_rendered=True)
    doc = record_to_doc(rec)
    assert "script_rendered" in RECORD_FIELDS and doc["script_rendered"] is True
    assert record_from_doc(doc) == rec


def test_a_record_stored_before_the_field_reads_false():
    doc = {k: v for k, v in record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="i")).items() if k != "script_rendered"}
    assert record_from_doc(doc).script_rendered is False


async def test_both_repositories_keep_it(mock_env_config):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="html", script_rendered=True,
                           modified_at=datetime(2026, 10, 13, tzinfo=timezone.utc))
    with eval_transactions():
        for repo in (InMemoryDriveIndexRepository(), make_eval_repo(EvalDb(), mock_env_config)):
            await repo.upsert_records([rec])
            assert (await repo.get_records(U_SCOPE, ["i"]))["i"].script_rendered is True
