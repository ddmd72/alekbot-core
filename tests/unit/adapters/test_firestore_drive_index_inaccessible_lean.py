"""The tick's lean read of the marked records (USER_DRIVE_INDEXING_RFC §26.2, review of S1): only the fields its
counters and parent map read, never the profile, keys or text the full projection carries."""
import pytest

from src.adapters.firestore_drive_index_repo import ACCESS_MARK_FIELDS
from src.domain.drive_access import access_counts
from src.domain.drive_index import DriveIndexRecord, RecordState
from src.domain.drive_search import is_not_read_yet

from tests.unit.adapters.firestore_drive_fakes import RECS, Db, executed, make_repo
from tests.unit.services.drive_index_fakes import U_SCOPE


@pytest.fixture
def repo(mock_env_config):
    db = Db()
    return make_repo(db, mock_env_config), db


async def test_the_lean_read_projects_the_mark_fields(repo):
    store, db = repo
    await store.inaccessible_records(U_SCOPE, 9, lean=True)
    projection = next(op for op in executed(db, RECS)[-1] if op[0] == "select")[1]
    assert set(projection) == set(ACCESS_MARK_FIELDS)
    assert {"profile", "about_keys", "lead_text"}.isdisjoint(projection)


def test_the_counters_need_no_other_field():
    full = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", parent_id="p", name="a.pdf", kind="pdf",
                            state=RecordState.PENDING, inaccessible_since=None, profile_state="ready", text_chars=9)
    lean = DriveIndexRecord(**{name: getattr(full, name) for name in ACCESS_MARK_FIELDS})
    assert is_not_read_yet(lean) == is_not_read_yet(full)
    assert access_counts([lean]) == access_counts([full])
