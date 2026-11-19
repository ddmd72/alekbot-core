"""The evaluating Firestore client (firestore_drive_eval_fakes) with the document id as a queryable field.

The mirror ledger pages on the document id (`order_by(FieldPath.document_id())`, a range on `__name__` against a
document reference), which the shared fake does not evaluate. `NameAwareDb` hands out collections that expose each
document's id as `__name__` while a query is evaluated and turn a reference value into its id; everything else is the
shared fake's. The executed queries keep their original ops, so the provisioning helpers apply to them.

`mirror_db(kit_db)` is the contract's database over the kits' backend seam (P12, tests/support/kit_backend.py): in the unit
suite `kit_db` is H1, which does not evaluate a range on `__name__`, so the contract keeps this drive fake there; in the cloud
gate it is the real test database, which does, with the ledger's composites (Δ217) of `development_drive_transcript_mirror`.
`on_the_fake(db)` is the fake's pass-through transactions (`eval_transactions`), and nothing on the real database.
"""
import contextlib
from typing import Any, Callable, ContextManager, Dict, List

from tests.support.firestore_eval import EvalDb as H1
from tests.unit.adapters.firestore_drive_eval_fakes import EvalCollection, EvalDb, eval_transactions

ROOTS = "eval_drive_mirror_roots"
LEDGER = "eval_drive_transcript_mirror"
GROUPS = {ROOTS: "development_drive_mirror_roots", LEDGER: "development_drive_transcript_mirror"}


class NameAwareCollection(EvalCollection):
    def evaluate(self, ops: List[tuple]) -> List[Any]:
        shown: List[tuple] = []
        for op in ops:
            if op[0] == "where" and op[1] == "__name__":
                op = ("where", "__name__", op[2], getattr(op[3], "id", op[3]))
            shown.append(op)
        saved: Dict[str, Dict[str, Any]] = self.docs
        self.docs = {doc_id: {**data, "__name__": doc_id} for doc_id, data in saved.items()}
        try:
            return super().evaluate(shown)
        finally:
            self.docs = saved


class NameAwareDb(EvalDb):
    def collection(self, name: str) -> EvalCollection:
        if name not in self.cols:
            self.cols[name] = NameAwareCollection(name)
        return self.cols[name]


def mirror_db(kit_db: Callable[..., Any]) -> Any:
    """The contract's database: `NameAwareDb` in the unit suite (where `kit_db` is H1), the test database in the cloud gate."""
    return NameAwareDb() if kit_db is H1 else kit_db(groups=GROUPS)


def on_the_fake(db: Any) -> ContextManager[Any]:
    """`eval_transactions()` for the drive fake; nothing for the real database (its transactions are the client's own)."""
    return eval_transactions() if isinstance(db, EvalDb) else contextlib.nullcontext()
