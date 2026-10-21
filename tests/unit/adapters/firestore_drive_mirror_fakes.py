"""The evaluating Firestore client (firestore_drive_eval_fakes) with the document id as a queryable field.

The mirror ledger pages on the document id (`order_by(FieldPath.document_id())`, a range on `__name__` against a
document reference), which the shared fake does not evaluate. `NameAwareDb` hands out collections that expose each
document's id as `__name__` while a query is evaluated and turn a reference value into its id; everything else is the
shared fake's. The executed queries keep their original ops, so the provisioning helpers apply to them.
"""
from typing import Any, Dict, List

from tests.unit.adapters.firestore_drive_eval_fakes import EvalCollection, EvalDb

ROOTS = "eval_drive_mirror_roots"
LEDGER = "eval_drive_transcript_mirror"


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
