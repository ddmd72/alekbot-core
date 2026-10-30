"""The pins' query shape and the mirror store's log lines (USER_DRIVE_INDEXING_RFC §27.9.3, §27.12; final review I4, M3;
Δ237). `pinned_keys` runs on every purge of every user, so it must need no composite index: equality filters only,
paged by the document id (single-field indexes serve it, the `transcript_records` practice), never a range or an
ordering on another field. A store error's log line carries the type and a digest, never the provider's message (which
holds the index-creation URL with the project id); the raised error keeps it for the smoke check."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_mirror import GONE, KIND_FILE, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, DeletionNotice, MirrorDoc

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
        yield db, FirestoreDriveMirrorRepository(db, env)


def _doc(item: str, **kw) -> MirrorDoc:
    drive = kw.pop("drive", "microsoft_onedrive:d1")          # the mirror's drive identity (MULTI_DRIVE_RFC §10)
    return MirrorDoc(user_id="u", drive_identity=drive, kind=KIND_FILE, source_item_id=item,
                     source_identity=f"microsoft_onedrive:src:{item}", **kw)


async def test_pinned_keys_needs_no_composite_index(run, monkeypatch):
    db, repo = run
    monkeypatch.setattr("src.adapters.firestore_drive_mirror_repo.PIN_PAGE", 2)
    docs = [_doc(f"z{n}", state=MIRROR_ARCHIVED, content_key=f"k{n % 3}") for n in range(5)]
    docs += [_doc(f"a{n}", state=MIRROR_DETACHED, content_key=f"d{n}", drive="microsoft_onedrive:d2") for n in range(3)]
    docs += [_doc(f"m{n}", state=MIRROR_ACTIVE, content_key=f"p{n}") for n in range(3)]
    docs += [_doc("x", state=MIRROR_ACTIVE, content_key="active-only")]
    for d in docs:
        await repo.save_doc(d)
    await repo.mark_gone("u", [DeletionNotice(f"m{n}", GONE, T, source_identity=f"microsoft_onedrive:src:m{n}")
                              for n in range(3)], drive_identity="microsoft_onedrive:d1")
    db.collection(LEDGER).executed.clear()
    keys = await repo.pinned_keys("u")
    assert keys == frozenset({"k0", "k1", "k2", "d0", "d1", "d2", "p0", "p1", "p2"})
    sent = db.collection(LEDGER).executed
    assert len(sent) > 2                                       # it paged (a page of 2)
    for ops in sent:
        for op in ops:
            if op[0] == "where" and op[1] != "__name__":
                assert op[2] in ("==", "in"), op                # equality only on fields
            if op[0] == "where" and op[1] == "__name__":
                assert op[2] == ">", op                         # the page cursor: the document id
            assert op[0] != "order_by" or op[1] == "__name__", op
            assert op[0] != "start_after", op
        fields = {op[1] for op in ops if op[0] == "where"}
        assert "content_key" not in fields and "drive_identity" not in fields


@pytest.mark.parametrize("error", [ServiceUnavailable("down at https://console.example.invalid/project/secret-proj"),
                                   FailedPrecondition("The query requires an index. You can create it here: "
                                                      "https://console.example.invalid/project/secret-proj/indexes")])
async def test_store_error_log_carries_no_provider_message(run, error):
    db, repo = run
    db.collection(LEDGER).fail = error
    with patch("src.adapters.firestore_drive_mirror_repo.logger") as log, \
            pytest.raises(DriveIndexStoreUnavailable):
        await repo.pinned_keys("u")
    lines = " ".join(str(a) for call in log.method_calls for a in call.args)
    assert lines and type(error).__name__ in lines and "message digest" in lines
    assert "secret-proj" not in lines and "console.example.invalid" not in lines
