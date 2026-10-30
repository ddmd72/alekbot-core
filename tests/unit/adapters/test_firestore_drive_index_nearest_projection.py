"""Card kNN reads the record fields only (USER_DRIVE_INDEXING_RFC §10.1): the 768-float `card_vector` of every hit is
never loaded. Verified live 2026-10-12: with a projection, Firestore drops the distance result field unless its name
is in the select list — so the list must carry `card_distance` too."""
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_vector_query import AsyncVectorQuery
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import (
    CARD_DISTANCE, CARD_VECTOR, RECORD_FIELDS, FirestoreDriveIndexRepository,
)

from tests.unit.adapters.firestore_drive_fakes import RECS, Db, executed, make_repo, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE


async def test_nearest_projects_the_record_fields_and_the_distance_before_the_search(mock_env_config):
    db = Db()
    repo = make_repo(db, mock_env_config)
    db.cols[RECS].respond = lambda ops: [snap({**rec_doc("a"), CARD_DISTANCE: 0.25})]
    hits = await repo.find_nearest_cards(U_SCOPE, [1.0, 0.0], 50, kind="pdf")
    assert [(r.item_id, d) for r, d in hits] == [("a", 0.25)]
    (ops,) = executed(db)
    names = [op[0] for op in ops]
    assert names == ["where", "where", "select", "find_nearest"]
    selected = ops[2][1]
    assert set(selected) == set(RECORD_FIELDS) | {CARD_DISTANCE}
    assert CARD_VECTOR not in selected


async def test_the_sent_query_selects_the_distance_field(monkeypatch, mock_env_config):
    """Through the SDK's own encoding: the projection and the kNN travel in one StructuredQuery."""
    sent = []

    async def capture(self, *args, **kwargs):
        sent.append(self._to_protobuf())
        return []

    monkeypatch.setattr(AsyncVectorQuery, "get", capture)
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    repo = FirestoreDriveIndexRepository(client, mock_env_config)
    assert await repo.find_nearest_cards(U_SCOPE, Vector([1.0]), 5) == []
    (query,) = sent
    selected = {f.field_path for f in query.select.fields}
    assert CARD_DISTANCE in selected and CARD_VECTOR not in selected
    assert query.find_nearest.distance_result_field == CARD_DISTANCE
