"""`unread` counts images that triage found to show text (USER_DRIVE_INDEXING_RFC §10.2, §24.5, review D-M4).

An image is a document once triage says it shows text. If its text then cannot be read (the reader failed, found no
text, the chunk cap refused it), it is unread like any other document. A photograph is never unread. The count holds
on every layer: the domain rule, the in-memory repository, Firestore's count aggregations, their composite indexes and
the smoke run."""
import importlib.util
from pathlib import Path

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS
from src.domain.drive_search import UNREAD_COUNTER, is_unread
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed
from tests.unit.adapters.test_firestore_drive_index_chunk_provisioning import _declared

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

_SMOKE = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"


def _image(item_id, verdict, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, kind="image", triage_verdict=verdict, **kw)


UNREAD_TEXT_IMAGES = [
    _image("failed", TriageVerdict.TEXT, state=RecordState.FAILED),
    _image("pending", TriageVerdict.TEXT, state=RecordState.PENDING),
    _image("no_text", TriageVerdict.TEXT, state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT),
    _image("capped", TriageVerdict.TEXT, state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP),
]
NEVER_UNREAD = [
    _image("photo", TriageVerdict.PHOTO, state=RecordState.METADATA_ONLY, reason=Reason.PHOTOGRAPH),
    _image("photo-failed", TriageVerdict.PHOTO, state=RecordState.FAILED),
    _image("untriaged", None, state=RecordState.PENDING),
    _image("read", TriageVerdict.TEXT, state=RecordState.INDEXED, indexed_generation=1),
]


class TestTheRule:
    @pytest.mark.parametrize("rec", UNREAD_TEXT_IMAGES, ids=lambda r: r.item_id)
    def test_a_text_image_that_cannot_be_read_is_unread_with_explicit_kinds(self, rec):
        assert is_unread(rec, SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS)

    @pytest.mark.parametrize("rec", NEVER_UNREAD, ids=lambda r: r.item_id)
    def test_photos_untriaged_and_read_images_are_not(self, rec):
        assert not is_unread(rec, SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS)


async def test_the_in_memory_count_includes_text_images():
    mem = InMemoryDriveIndexRepository()
    await mem.upsert_records(UNREAD_TEXT_IMAGES + NEVER_UNREAD + [
        DriveIndexRecord(user_id="u", connection_id="u", item_id="doc", kind="pdf", state=RecordState.FAILED)])
    assert await mem.count_unread("u", SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS) == 5
    assert await mem.count_unread("u", SLICE2_CONTENT_KINDS) == 1   # no triaged kinds asked for


class TestFirestore:
    @pytest.fixture
    def db(self):
        return ChunkDb()

    async def test_one_count_per_unread_class_for_the_triaged_kinds_with_the_text_verdict(self, db, mock_env_config):
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, {"pdf"}, {"image"})
        queries = executed(db, RECS)
        assert len(queries) == 12
        triaged = [ops for ops in queries if ("where", "kind", "in", ["image"]) in ops]
        assert len(triaged) == 6 and all(("where", "triage_verdict", "==", "text") in ops for ops in triaged)
        documents = [ops for ops in queries if ("where", "kind", "in", ["pdf"]) in ops]
        assert len(documents) == 6 and not any(op[1] == "triage_verdict" for ops in documents for op in ops)

    async def test_a_kind_both_admitted_and_triaged_is_counted_once(self, db, mock_env_config):
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, {"pdf", "image"}, {"image"})
        assert len(executed(db, RECS)) == 6

    async def test_every_triaged_count_has_its_composite_index(self, db, mock_env_config):
        repo = make_chunk_repo(db, mock_env_config)
        await repo.count_unread(U_SCOPE, {"pdf"}, {"image"})
        declared = _declared("development_drive_index_records")
        for ops in executed(db, RECS):
            prefix = {(op[1], "EQ") for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "in")}
            assert any({(p, "EQ") for p, _ in i} == prefix for i in declared), ops


async def test_the_tick_counts_a_text_image_the_reader_could_not_read(admitted):
    w = ContentWorld()
    receipt = w.add_image("IMG_2.jpg")
    w.add_image("IMG_3.jpg")                                    # a photograph: never unread
    w.triage.verdicts = {receipt: (TriageVerdict.TEXT, "")}
    w.reader.outcomes["IMG_2.jpg"] = ExtractionOutcome(status=ExtractionStatus.ERROR)
    for _ in range(MAX_ATTEMPTS + 1):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    await w.settle()
    assert w.rec(receipt).state is RecordState.FAILED
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.counters[UNREAD_COUNTER] == 1


async def test_the_smoke_run_covers_the_triaged_counts(mock_env_config):
    spec = importlib.util.spec_from_file_location("drive_index_smoke", _SMOKE)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    db = ChunkDb()
    assert await smoke.run_smoke(make_chunk_repo(db, mock_env_config), "smoke-user") == []
    triaged = [ops for ops in db.cols[RECS].executed if ("where", "triage_verdict", "==", "text") in ops]
    assert len(triaged) == 6 and all(ops[-1][0] == "count" for ops in triaged)
