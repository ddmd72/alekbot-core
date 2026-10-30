"""`mark_gone(..., drive_identity=)` (USER_DRIVE_INDEXING_RFC §27.9.3, §27.9.4; T2 Task 20B carry; MULTI_DRIVE_RFC §10):
a notice of the connected drive must not mark another drive's document of the same source item.
Run against the in-memory reference and the Firestore adapter on the evaluating fake."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_mirror import GONE, KIND_FILE, KIND_FOLDER, DeletionNotice, MirrorDoc

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "firestore"])
def repo(request):
    if request.param == "memory":
        yield InMemoryDriveMirrorRepository()
        return
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    with eval_transactions():
        yield FirestoreDriveMirrorRepository(NameAwareDb(), env)


OLD, D1 = "microsoft_onedrive:old", "microsoft_onedrive:d1"          # mirror drive identities (MULTI_DRIVE_RFC §7.2)
SA, SB = "microsoft_onedrive:src:a", "microsoft_onedrive:src:b"      # source item identities


async def test_a_document_of_another_drive_is_left_unmarked(repo):
    a = MirrorDoc(user_id="u", drive_identity=OLD, kind=KIND_FILE, source_item_id="a", source_identity=SA)
    b = MirrorDoc(user_id="u", drive_identity=D1, kind=KIND_FOLDER, source_item_id="b", source_identity=SB)
    await repo.save_doc(a)
    await repo.save_doc(b)
    count = await repo.mark_gone("u", [DeletionNotice("a", GONE, T, source_identity=SA),
                                       DeletionNotice("b", GONE, T, source_identity=SB)], drive_identity=D1)
    assert count == 1
    docs = await repo.get_docs("u", [a.key, b.key])
    assert docs[a.key].gone_pending is False
    assert docs[b.key].gone_pending is True


async def test_without_a_drive_id_every_drive_is_marked(repo):
    await repo.save_doc(MirrorDoc(user_id="u", drive_id="old", kind=KIND_FILE, source_item_id="a"))
    assert await repo.mark_gone("u", [DeletionNotice("a", GONE, T)]) == 1
    assert (await repo.get_docs("u", ["u:file:a"]))["u:file:a"].gone_pending is True
