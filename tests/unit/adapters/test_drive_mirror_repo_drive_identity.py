"""The transcript mirror's roots and ledger by drive identity (MULTI_DRIVE_RFC §10, I5, I6), on both adapters (the
in-memory reference and the Firestore adapter on the evaluating fake). A root is keyed by its drive identity, so a
new connection of the same drive finds it; a file's document is keyed by the mirror's drive identity and the source's
identity, so two connections that see one source item each keep their own document; a deletion mark names exactly the
documents of the given mirror. Technical ids only."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_mirror import (
    GONE, KIND_FILE, KIND_FOLDER, DeletionNotice, MirrorDoc, MirrorRoot, ledger_key, mirror_root_doc_id,
)

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb

T = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
U = "usr-1"
DRIVE_A, DRIVE_B = "fake_a:drive-1", "fake_b:drive-2"
SOURCE = "fake_a:drive-1:item-1"   # one source item both mirrors see (a file shared to two accounts)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request):
    if request.param == "memory":
        yield InMemoryDriveMirrorRepository(), None
        return
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    db = NameAwareDb()
    with eval_transactions():
        yield FirestoreDriveMirrorRepository(db, env), db


def _doc(drive: str, kind: str = KIND_FILE, **kw) -> MirrorDoc:
    return MirrorDoc(user_id=U, drive_identity=drive, kind=kind, source_item_id="item-1", source_identity=SOURCE, **kw)


async def test_a_root_is_keyed_by_its_drive_identity(adapter):
    repo, db = adapter
    await repo.save_root(MirrorRoot(user_id=U, drive_identity=DRIVE_A, root_item_id="r1", root_name="_Transcripts"))
    assert (await repo.get_root(U, DRIVE_A)).root_item_id == "r1"
    assert await repo.get_root(U, DRIVE_B) is None
    assert [r.drive_identity for r in await repo.roots(U)] == [DRIVE_A]
    if db is not None:
        assert set(db.collection(ROOTS).docs) == {mirror_root_doc_id(U, DRIVE_A)}
    with pytest.raises(ValueError):
        await repo.save_root(MirrorRoot(user_id=U, drive_identity="", root_item_id="r2", root_name="_Transcripts"))


async def test_two_mirrors_of_one_source_keep_two_documents(adapter):
    repo, _ = adapter
    a, b = _doc(DRIVE_A, mirror_item_id="m-a"), _doc(DRIVE_B, mirror_item_id="m-b")
    await repo.save_doc(a)
    await repo.save_doc(b)
    assert a.key == ledger_key(U, KIND_FILE, DRIVE_A, SOURCE) != b.key
    found = await repo.get_docs(U, [a.key, b.key])
    assert {d.drive_identity: d.mirror_item_id for d in found.values()} == {DRIVE_A: "m-a", DRIVE_B: "m-b"}
    assert [d.mirror_item_id for d in await repo.scan_page(U, DRIVE_A, None, 10)] == ["m-a"]


async def test_a_deletion_mark_names_only_the_given_mirrors_documents(adapter):
    repo, _ = adapter
    await repo.save_doc(_doc(DRIVE_A))
    await repo.save_doc(_doc(DRIVE_A, kind=KIND_FOLDER))
    await repo.save_doc(_doc(DRIVE_B))
    marked = await repo.mark_gone(U, [DeletionNotice("item-1", GONE, T, source_identity=SOURCE),
                                      DeletionNotice("item-2", GONE, T)], drive_identity=DRIVE_A)
    assert marked == 2   # the file and the folder document of mirror A; a notice without an identity names none
    docs = await repo.get_docs(U, [_doc(DRIVE_A).key, _doc(DRIVE_B).key])
    assert docs[_doc(DRIVE_A).key].gone_pending and not docs[_doc(DRIVE_B).key].gone_pending
    with pytest.raises(ValueError):
        await repo.mark_gone(U, [DeletionNotice("item-1", GONE, T, source_identity=SOURCE)], drive_identity="")
