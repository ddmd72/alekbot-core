"""Additions to the evaluating Firestore fake (firestore_drive_eval_fakes) for the transcript documents: `create`
(AlreadyExists on a present document) and an `update` of a missing document raising NotFound as the SDK does.
Patched in from here so the shared fake stays as it is."""
from contextlib import contextmanager
from typing import Any, Dict, Iterator
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, NotFound

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDocRef, _write


async def _create(self: EvalDocRef, data: Dict[str, Any]) -> None:
    if self.id in self.col.docs:
        raise AlreadyExists(self.id)
    _write(self.col, self.id, data)


_plain_update = EvalDocRef.update


async def _update(self: EvalDocRef, data: Dict[str, Any]) -> None:
    if self.id not in self.col.docs:
        raise NotFound(self.id)
    await _plain_update(self, data)


@contextmanager
def transcript_doc_fakes() -> Iterator[None]:
    with patch.object(EvalDocRef, "create", _create, create=True), patch.object(EvalDocRef, "update", _update):
        yield
