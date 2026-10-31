"""A recording Firestore client for FirestoreDriveIndexRepository wire tests: queries are immutable op lists like
the SDK's, `get`/`stream` log them, and each collection answers through its `respond(ops)` callable."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from google.cloud.firestore_v1.types import StructuredQuery

from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, FirestoreDriveIndexRepository, record_to_doc
from src.domain.drive_index import DriveIndexRecord

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
TS_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)
RECS = "test_drive_index_records"
STATES = "test_drive_sync_state"
PROJECTION = ("select", RECORD_FIELDS)
_RANGE_OPS = {"<", "<=", ">", ">="}


def snap(data=None, doc_id=None):
    s = MagicMock()
    s.exists = data is not None
    s.to_dict.return_value = dict(data) if data is not None else None
    s.id = doc_id
    s.reference = SimpleNamespace(doc_id=doc_id)
    return s


def rec_doc(item_id, **kw):
    return record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, **kw))


class Query:
    """An immutable query chain like the SDK's; `get`/`stream` log the ops and ask the collection to answer."""

    def __init__(self, col, ops):
        self.col, self.ops = col, ops

    def _then(self, op):
        return Query(self.col, self.ops + [op])

    def where(self, *, filter):
        op = filter.op_string   # `== None` arrives as the SDK's unary IS_NULL operator
        op = op if isinstance(op, str) else StructuredQuery.UnaryFilter.Operator(op).name
        return self._then(("where", filter.field_path, op, filter.value))

    def order_by(self, field, direction="ASCENDING"):
        return self._then(("order_by", field, direction))

    def limit(self, n):
        return self._then(("limit", n))

    def select(self, paths):
        return self._then(("select", tuple(paths)))

    def find_nearest(self, **kw):
        return self._then(("find_nearest", kw))

    def count(self, alias=None):
        return self._then(("count", alias))

    async def get(self):
        self.col.executed.append(self.ops)
        return self.col.respond(self.ops)

    async def stream(self):
        for s in await self.get():
            yield s


class Collection:
    def __init__(self, name):
        self.name = name
        self.executed = []
        self.respond = lambda ops: [[SimpleNamespace(alias="n", value=0)]] if ops[-1][0] == "count" else []
        self.docs = {}

    def where(self, *, filter):
        return Query(self, []).where(filter=filter)

    def select(self, paths):
        return Query(self, []).select(paths)

    def document(self, doc_id):
        if doc_id not in self.docs:
            doc = MagicMock()
            doc.doc_id = doc_id
            doc.get = AsyncMock(return_value=snap(None, doc_id))
            doc.set, doc.update, doc.delete = AsyncMock(), AsyncMock(), AsyncMock()
            self.docs[doc_id] = doc
        return self.docs[doc_id]


class Db:
    def __init__(self):
        self.cols = {RECS: Collection(RECS), STATES: Collection(STATES)}
        self.batches = []
        self.stored = {}            # doc id -> data, served by get_all
        self.get_all_calls = []
        self.tx = MagicMock()

    def collection(self, name):
        return self.cols[name]

    def batch(self):
        b = MagicMock()
        b.commit = AsyncMock()
        self.batches.append(b)
        return b

    def transaction(self):
        return self.tx

    async def get_all(self, refs, field_paths=None):
        self.get_all_calls.append(([r.doc_id for r in refs], field_paths))
        for ref in refs:
            yield snap(self.stored.get(ref.doc_id), ref.doc_id)


def assert_valid_order(ops):
    """Every query keeps its range field as the first ordering — valid under any Firestore ordering rule."""
    ranged = [op[1] for op in ops if op[0] == "where" and op[2] in _RANGE_OPS]
    orders = [op[1] for op in ops if op[0] == "order_by"]
    assert len(set(ranged)) <= 1, ops
    if ranged and orders:
        assert orders[0] == ranged[0], ops


def executed(db, name=RECS):
    for ops in db.cols[name].executed:
        assert_valid_order(ops)
    return db.cols[name].executed


def make_repo(db, env_config):
    env_config.drive_index_records_collection = RECS
    env_config.drive_sync_state_collection = STATES
    return FirestoreDriveIndexRepository(db, env_config)
