"""`transcript_records`: the stage's read of in-flight (`working`) and copy-waiting (`waiting`) records, which neither the
ready queue nor the wake-ups return (plan Task 12, carries on woken mid-job parks and crash exits). Both adapters; the
Firestore read is equality-only, so single-field indexes serve it and no composite is declared."""
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import _exempt, _required
from tests.unit.services.drive_index_fakes import U_SCOPE

LIVE_RECS = "development_drive_index_records"


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = EvalDb()
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


def _rec(item_id, user="u", **kw):
    return DriveIndexRecord(user, item_id, connection_id=user, **kw)   # one connection per user here (U_SCOPE)


async def test_records_in_one_transcript_state_by_item(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        _rec("w2", transcript_state="working"), _rec("w1", transcript_state="working"),
        _rec("q", transcript_state="waiting"), _rec("r", transcript_state="ready"), _rec("n"),
        _rec("other", user="v", transcript_state="working"),
    ])
    assert [r.item_id for r in await repo.transcript_records(U_SCOPE, "working", 10)] == ["w1", "w2"]
    assert [r.item_id for r in await repo.transcript_records(U_SCOPE, "working", 1)] == ["w1"]
    assert [r.item_id for r in await repo.transcript_records(U_SCOPE, "waiting", 10)] == ["q"]
    assert await repo.transcript_records(U_SCOPE, "parked", 10) == []


async def test_the_firestore_read_is_equality_only(adapter):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore wire only")
    await repo.upsert_records([_rec("w", transcript_state="working")])
    db.cols[RECS].executed.clear()
    await repo.transcript_records(U_SCOPE, "working", 5)
    (ops,) = db.cols[RECS].executed
    assert _required(ops) is None   # two equality filters, no ordering: single-field indexes serve it
    assert not {"user_id", "transcript_state"} & _exempt(LIVE_RECS)
