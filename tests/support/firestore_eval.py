"""The shared evaluating Firestore fake: stores documents and evaluates the queries a repository sends.

Built on the drive suite's evaluator (`tests/unit/adapters/firestore_drive_eval_fakes.py`, left untouched): filters
(`==`, null, ranges, `in`, `array_contains*`), Firestore's value-type ordering with the implicit `__name__` tie-break,
`select` projections, cursors, `limit`, aggregations, batches, transactions, merges. This module closes the gaps other
repositories hit, and stays generic (no entity-specific logic):

- `find_nearest` without `distance_result_field` (the field is then not added), with a `distance_threshold`
  (COSINE: documents at distance <= threshold are kept), and with `Vector` or plain-list query vectors;
- `update()` on a missing document raises `google.api_core.exceptions.NotFound`, as Firestore does.

Not a Firestore emulator: only what the repositories in this codebase use. Every executed query is logged in
`EvalCollection.executed` in the recording fakes' op format.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from google.api_core.exceptions import NotFound
from google.cloud.firestore_v1.vector import Vector

from tests.unit.adapters import firestore_drive_eval_fakes as _base
from tests.unit.adapters.firestore_drive_eval_fakes import EvalSnap, _project, _write

__all__ = ["EvalDb", "EvalCollection", "EvalDocRef", "EvalSnap", "stored_vector"]


def stored_vector(value: Any) -> List[float]:
    """A stored `Vector` (or list) as a plain list of floats, for assertions."""
    return [float(x) for x in value]


class EvalDocRef(_base.EvalDocRef):
    async def update(self, data: Dict[str, Any]) -> None:
        if self.id not in self.col.docs:
            raise NotFound(f"No document to update: {self.col.name}/{self.id}")
        _write(self.col, self.id, data, merge=list(data))


class EvalCollection(_base.EvalCollection):
    def document(self, doc_id: str) -> EvalDocRef:
        return EvalDocRef(self, doc_id)

    def find_nearest(self, **kw: Any) -> _base.EvalQuery:
        return _base.EvalQuery(self, []).find_nearest(**kw)

    def limit(self, n: int) -> _base.EvalQuery:
        return _base.EvalQuery(self, []).limit(n)

    async def get(self) -> List[Any]:
        return await _base.EvalQuery(self, []).get()

    def stream(self):   # type: ignore[no-untyped-def]
        return _base.EvalQuery(self, []).stream()

    def _nearest(self, rows: List[tuple], spec: Dict[str, Any], paths: Optional[List[str]]) -> List[EvalSnap]:
        field, query = spec["vector_field"], [float(x) for x in spec["query_vector"]]
        result_field = spec.get("distance_result_field")
        threshold = spec.get("distance_threshold")
        scored = []
        for doc_id, data in rows:
            vector = data.get(field)
            if not isinstance(vector, Vector):
                continue
            v = [float(x) for x in vector]
            dot = sum(a * b for a, b in zip(v, query))
            na, nb = math.sqrt(sum(a * a for a in v)), math.sqrt(sum(b * b for b in query))
            distance = 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)
            if threshold is not None and distance > threshold:
                continue
            scored.append((distance, doc_id, data))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = []
        for distance, doc_id, data in scored[:spec["limit"]]:
            shown = {**data, result_field: distance} if result_field else dict(data)
            out.append(EvalSnap(self.document(doc_id), _project(shown, paths)))
        return out


class EvalDb(_base.EvalDb):
    def collection(self, name: str) -> EvalCollection:
        if name not in self.cols:
            self.cols[name] = EvalCollection(name)
        return self.cols[name]  # type: ignore[return-value]
