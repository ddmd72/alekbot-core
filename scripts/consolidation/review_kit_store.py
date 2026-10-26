"""The review's question store for the dry-run kit (Facts S4 plan T12a, F20): the real `FirestoreFactReviewStore` over an
in-memory database, so every transition, claim and state mark is production's own code and nothing is written live.

Why not a hand-written fake: the store's rules (a claim moves the state's pointer, a close clears it only when it names
the question, which fields each mark touches, what `requeue_front` records) live in the adapter. A second copy in a script
would drift. Here the adapter runs unchanged; only its transaction runner (`_in_transaction`) and the database it talks to
are the kit's.

Reads: `load_account` copies the account's open questions and review state from a real store, through that store's two
read methods only; nothing is ever written back. A question or state the run writes lands in memory and is listed in
`writes`. The mini database knows exactly the calls the adapter makes (`document`, `get`, `create`, `set` with `merge`,
`delete`, `where` with `==`, `in` and `<`, `get_all`, `stream`, `transaction`); another call or operator raises
NotImplementedError, so a new adapter query shows up here as a failing kit test instead of a wrong answer.
"""
import copy
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Tuple

from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_fact_review_store import (
    FirestoreFactReviewStore,
    question_document,
    question_from,
    state_document,
    state_from,
)
from src.config.environment import EnvironmentConfig
from src.domain.fact_review_question import FactReviewQuestion
from src.domain.fact_review_state import FactReviewState
from src.ports.fact_review_store import FactReviewStore


class _Snapshot:
    def __init__(self, doc_id: str, data: Optional[Dict[str, Any]]) -> None:
        self.id = doc_id
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self._data) if self._data is not None else None


class _Query:
    """A collection filtered by `where(filter=FieldFilter(...))` clauses, all of which must hold."""

    def __init__(self, collection: "_Collection", filters: Tuple[Any, ...] = ()) -> None:
        self._collection = collection
        self._filters = filters

    def where(self, *, filter: Any) -> "_Query":     # the client's keyword, so the adapter's call reads the same
        return _Query(self._collection, (*self._filters, filter))

    async def get(self, transaction: Any = None) -> List[_Snapshot]:
        return [
            _Snapshot(doc_id, data) for doc_id, data in list(self._collection.docs.items())
            if all(_holds(data, clause) for clause in self._filters)
        ]


def _holds(data: Dict[str, Any], clause: Any) -> bool:
    value = data.get(clause.field_path)
    if clause.op_string == "==":
        return bool(value == clause.value)
    if clause.op_string == "in":
        return value in clause.value
    if clause.op_string == "<":
        return value is not None and bool(value < clause.value)
    raise NotImplementedError(f"the kit's in-memory database does not evaluate {clause.op_string!r}")


class _Document:
    def __init__(self, collection: "_Collection", doc_id: str) -> None:
        self._collection = collection
        self.id = doc_id

    async def get(self, transaction: Any = None) -> _Snapshot:
        data = self._collection.docs.get(self.id)
        return _Snapshot(self.id, copy.deepcopy(data) if data is not None else None)

    async def create(self, data: Dict[str, Any]) -> None:
        if self.id in self._collection.docs:
            raise AlreadyExists(f"{self._collection.name}/{self.id} exists")
        self._collection.put(self.id, data)

    async def delete(self) -> None:
        self._collection.docs.pop(self.id, None)


class _Collection(_Query):
    def __init__(self, name: str, on_write: Any) -> None:
        super().__init__(self)
        self.name = name
        self.docs: Dict[str, Dict[str, Any]] = {}
        self._on_write = on_write

    def document(self, doc_id: str) -> _Document:
        return _Document(self, doc_id)

    def put(self, doc_id: str, data: Dict[str, Any], merge: Optional[Sequence[str]] = None) -> None:
        """A write: the document replaced, or, with `merge`, only those fields (a missing document is created from them)."""
        if merge is None:
            self.docs[doc_id] = copy.deepcopy(data)
        else:
            self.docs[doc_id] = {**self.docs.get(doc_id, {}), **{name: copy.deepcopy(data[name]) for name in merge}}
        self._on_write(self.name, doc_id, sorted(data) if merge is None else sorted(merge))

    async def stream(self) -> AsyncIterator[_Snapshot]:
        for doc_id, data in list(self.docs.items()):
            yield _Snapshot(doc_id, copy.deepcopy(data))


class _Transaction:
    """Writes are applied when the body returns, as a commit does; a body that raises writes nothing."""

    def __init__(self) -> None:
        self._writes: List[Tuple[_Document, Dict[str, Any], Optional[Sequence[str]]]] = []

    def set(self, ref: _Document, data: Dict[str, Any], merge: Optional[Sequence[str]] = None) -> None:
        self._writes.append((ref, data, merge))

    def commit(self) -> None:
        for ref, data, merge in self._writes:
            ref._collection.put(ref.id, data, merge)


class MemoryFirestore:
    """The part of the Firestore client the review store uses."""

    def __init__(self, on_write: Any) -> None:
        self._on_write = on_write
        self._collections: Dict[str, _Collection] = {}

    def collection(self, name: str) -> _Collection:
        return self._collections.setdefault(name, _Collection(name, self._on_write))

    async def get_all(self, refs: Iterable[_Document]) -> AsyncIterator[_Snapshot]:
        for ref in refs:
            yield await ref.get()

    def transaction(self) -> _Transaction:
        return _Transaction()


class RecordingReviewStore(FirestoreFactReviewStore):
    """`FactReviewStore` for a dry run: production's adapter on memory. `writes` lists every document write as
    `{"collection", "doc_id", "fields"}`, in order; seeding by `load_account` is not a write."""

    def __init__(self, env_config: Optional[EnvironmentConfig] = None) -> None:
        self.writes: List[Dict[str, Any]] = []
        self.memory = MemoryFirestore(self._record)
        self._env = env_config or EnvironmentConfig()
        super().__init__(self.memory, self._env)

    def _record(self, collection: str, doc_id: str, fields: List[str]) -> None:
        self.writes.append({"collection": collection, "doc_id": doc_id, "fields": fields})

    async def load_account(self, real: FactReviewStore, account_id: str) -> None:
        """Copy the account's open questions and its review state from `real` (its two read methods; nothing is written
        to it). Replaces whatever this store held for the account."""
        questions = self.memory.collection(self._env.fact_review_questions_collection)
        states = self.memory.collection(self._env.fact_review_state_collection)
        for doc_id in [i for i, d in questions.docs.items() if d.get("account_id") == account_id]:
            del questions.docs[doc_id]
        states.docs.pop(account_id, None)
        for question in await real.questions_for_account(account_id):
            questions.docs[question.question_id] = question_document(question)
        state = await real.get_state(account_id)
        if state is not None:
            states.docs[account_id] = state_document(state)

    # What the run left, as models (the test and the benches read these).
    def questions(self) -> List[FactReviewQuestion]:
        return [question_from(d) for d in self.memory.collection(self._env.fact_review_questions_collection).docs.values()]

    def state(self, account_id: str) -> Optional[FactReviewState]:
        data = self.memory.collection(self._env.fact_review_state_collection).docs.get(account_id)
        return state_from(data) if data is not None else None

    async def _in_transaction(self, body: Any, what: str) -> Any:
        transaction = self.memory.transaction()
        result = await body(transaction)
        transaction.commit()
        return result

    async def purge_overdue(self, before: datetime) -> int:
        raise NotImplementedError("the kit's store has no expiry to count")
