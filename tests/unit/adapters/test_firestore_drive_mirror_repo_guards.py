"""Guards of the mirror adapter (Task 20A fix round 1): chunked mark writes, no drift from the index adapter's error and
timestamp handling, a codec that refuses an unknown field shape, a write-path backend error."""
import typing
from dataclasses import fields
from datetime import datetime, timezone
from typing import get_type_hints
from unittest.mock import MagicMock

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import ServiceUnavailable

import src.adapters.firestore_drive_index_repo as index_repo
import src.adapters.firestore_drive_mirror_repo as mirror_repo
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_index_params import RECORD_WRITE_BATCH
from src.domain.drive_mirror import GONE, DeletionNotice, MirrorDoc, MirrorRoot

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def run():
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    db = NameAwareDb()
    with eval_transactions():
        yield db, mirror_repo.FirestoreDriveMirrorRepository(db, env)


DRIVE = "microsoft_onedrive:d"     # the mirror's drive identity (MULTI_DRIVE_RFC §10)


@pytest.mark.slow
async def test_mark_gone_commits_in_chunks(run):
    db, repo = run
    n = RECORD_WRITE_BATCH * 2 + 50
    for i in range(n):
        await repo.save_doc(MirrorDoc(user_id="u", drive_identity=DRIVE, kind="file", source_item_id=f"i{i}",
                                      source_identity=f"{DRIVE}:i{i}"))
    assert await repo.mark_gone("u", [DeletionNotice(f"i{i}", GONE, T, source_identity=f"{DRIVE}:i{i}") for i in range(n)],
                                drive_identity=DRIVE) == n
    assert [len(c) for c in db.commits] == [RECORD_WRITE_BATCH, RECORD_WRITE_BATCH, 50]
    assert all(d["gone_pending"] for d in db.collection(LEDGER).docs.values())


def test_error_classes_and_timestamp_reader_do_not_drift_from_the_index_adapter():
    assert mirror_repo._TRANSIENT == index_repo._TRANSIENT and mirror_repo._MISSING_INDEX == index_repo._MISSING_INDEX
    nanos = DatetimeWithNanoseconds(2026, 10, 21, 9, 0, 0, 5, tzinfo=timezone.utc)
    for value in (nanos, datetime(2026, 10, 21, 9, 0, 0, 5), None):
        a, b = mirror_repo._dt(value), index_repo._dt(value)
        assert a == b and (a is None or type(a) is datetime)


def test_every_field_has_a_supported_codec_shape():
    ok = {str, int, float, bool, typing.Optional[datetime], typing.Tuple[str, ...]}
    for cls in (MirrorDoc, MirrorRoot):
        hints = get_type_hints(cls)
        assert [f.name for f in fields(cls) if hints[f.name] not in ok] == []


async def test_delete_doc_checks_the_documents_owner(run):
    db, repo = run
    doc = MirrorDoc(user_id="u", drive_identity=DRIVE, kind="file", source_item_id="a", source_identity=f"{DRIVE}:a")
    await repo.save_doc(doc)
    await repo.delete_doc("v", doc.key)         # a key that is another user's document
    assert doc.key in db.collection(LEDGER).docs
    await repo.delete_doc("u", doc.key)
    assert doc.key not in db.collection(LEDGER).docs


async def test_a_write_path_backend_error_is_store_unavailable(run, monkeypatch):
    db, repo = run

    async def boom(*a, **k):
        raise ServiceUnavailable("down")

    monkeypatch.setattr(type(db.collection(LEDGER).document("x")), "set", boom)
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.save_doc(MirrorDoc(user_id="u", drive_identity=DRIVE, kind="file", source_item_id="a", source_identity=f"{DRIVE}:a"))
