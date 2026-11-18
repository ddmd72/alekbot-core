"""The backend seam of the store kits (TEST_COVERAGE_SYSTEMIC_RFC §3 H2, §4 "What runs there", P12).

A kit builds its store over `kit_db(indexes=..., groups=...)`, the factory this fixture gives, with `EvalDb`'s arguments:

* in the unit suite (`tests/conftest.py` imports the fixture below) `kit_db` is `EvalDb` itself: offline, unchanged;
* in the cloud gate the same kit module is collected again by `tests/cloud/test_kits_cloud.py` and `tests/cloud/conftest.py`
  overrides `kit_db` with a `RealKitBackend`: every call returns a `RealKitDb`, the real `AsyncClient` on the test
  database behind the guards of that conftest.

`RealKitDb` maps each top-level collection name: a name the kit maps in `groups` to a live `development_*` group, or a
`development_*` name itself, becomes that group rendered to the `test_` prefix (`test_sessions`), whose composite indexes
`config/firestore.indexes.json` declares are created on the test database before first use; any other name becomes
`test_kits_<name>`. The names are fixed, not per run, because an index belongs to a collection id. Every collection a test
touches is purged (recursively) before its first use and after the test, so each test starts empty; one runner at a time.

Raw access stays the kit's: `db.collection(name).docs` is the stored documents as a dict. On the fake it is the fake's dict;
here it is a fresh synchronous read of the collection (`RawDocs`): assigning, deleting or popping a document, and setting,
deleting or popping a top-level field of one (`RawDoc`), is written through to Firestore. A nested value changed in place
is not written. Every other attribute is the real client's, so a fake-only hook (`interfere_on_txn_read`, `versions`, a
patched `EvalDocRef`) fails or does nothing here: such a test carries `@pytest.mark.h1_only("<why>")` and is skipped in
the cloud run.
"""
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

import pytest

from tests.support.cloud_indexes import LIVE_PREFIX, render_indexes, rendered_group
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
UNGROUPED_PREFIX = "test_kits_"


@pytest.fixture
def kit_db() -> Callable[..., Any]:
    """The kit's database factory: `EvalDb` in the unit suite (overridden in `tests/cloud/conftest.py`)."""
    return EvalDb


def live_group(name: str, groups: Optional[Dict[str, str]]) -> Optional[str]:
    """The live `development_*` group a kit's top-level collection name stands for, or None."""
    group = (groups or {}).get(name, name)
    return group if group.startswith(LIVE_PREFIX) else None


def real_name(name: str, groups: Optional[Dict[str, str]]) -> str:
    """The test database's name for a kit's top-level collection name (module docstring)."""
    group = live_group(name, groups)
    return rendered_group(group) if group else UNGROUPED_PREFIX + name     # type: ignore[return-value]


class RawDoc(dict):
    """One stored document; a top-level field set, deleted or popped is written through (the whole document is rewritten)."""

    def __init__(self, ref: Any, data: Optional[Dict[str, Any]]) -> None:
        super().__init__(data or {})
        self._ref = ref

    def _write(self) -> None:
        self._ref.set(dict(self))

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._write()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._write()

    def pop(self, key: str, *default: Any) -> Any:
        value = super().pop(key, *default)
        self._write()
        return value

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        self._write()

    def setdefault(self, *_a: Any) -> Any:
        raise NotImplementedError("RawDoc: assign the field instead")

    def popitem(self) -> Any:
        raise NotImplementedError("RawDoc: pop a named field instead")

    def clear(self) -> None:
        raise NotImplementedError("RawDoc: assign the document in RawDocs instead")


class RawDocs(dict):
    """The documents of one collection, read now (doc id -> `RawDoc`); assigning, deleting or popping one is written through."""

    def __init__(self, col: Any) -> None:
        super().__init__({snap.id: RawDoc(snap.reference, snap.to_dict()) for snap in col.stream()})
        self._col = col

    def __setitem__(self, doc_id: str, data: Dict[str, Any]) -> None:
        ref = self._col.document(doc_id)
        ref.set(dict(data))
        super().__setitem__(doc_id, RawDoc(ref, dict(data)))

    def __delitem__(self, doc_id: str) -> None:
        super().__delitem__(doc_id)
        self._col.document(doc_id).delete()

    def pop(self, doc_id: str, *default: Any) -> Any:
        if doc_id in self:
            self._col.document(doc_id).delete()
        return super().pop(doc_id, *default)

    def clear(self) -> None:
        for doc_id in list(self):
            del self[doc_id]

    def update(self, *args: Any, **kwargs: Any) -> None:
        for doc_id, data in dict(*args, **kwargs).items():
            self[doc_id] = data

    def setdefault(self, *_a: Any) -> Any:
        raise NotImplementedError("RawDocs: assign the document instead")

    def popitem(self) -> Any:
        raise NotImplementedError("RawDocs: pop a named document instead")


class RealCollection:
    """A real `AsyncCollectionReference` plus the kit's raw view `docs`; every other attribute is the reference's."""

    def __init__(self, backend: "RealKitBackend", path: str) -> None:
        self._backend = backend
        self._path = path
        self._ref = backend.client.collection(path)

    @property
    def docs(self) -> RawDocs:
        return RawDocs(self._backend.sync.collection(self._path))

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._ref, attr)


class RealKitDb:
    """What `kit_db(...)` returns in the cloud gate: the real `AsyncClient` with the kit's collection names mapped."""

    def __init__(self, backend: "RealKitBackend", groups: Optional[Dict[str, str]]) -> None:
        self._backend = backend
        self._groups = dict(groups or {})

    def collection(self, *path: str) -> RealCollection:
        segments = "/".join(path).split("/")
        segments[0] = self._backend.touch(segments[0], self._groups)
        return RealCollection(self._backend, "/".join(segments))

    def collection_group(self, name: str) -> Any:
        mapped = self._backend.touch(name, self._groups) if live_group(name, self._groups) else name
        return self._backend.client.collection_group(mapped)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._backend.client, attr)


class RealKitBackend:
    """The cloud `kit_db`: a factory of `RealKitDb` over one `AsyncClient` (async, what the store gets) and one sync
    `Client` (the raw view and the purge), both already checked against the test database by the caller. `ensure(group,
    indexes)` creates and waits for a group's indexes (`ensure_indexes` of `tests/cloud/conftest.py`)."""

    def __init__(self, client: Any, sync: Any, ensure: Callable[[str, List[Dict[str, Any]]], None],
                 indexes: Union[str, Path, List[Dict[str, Any]]] = INDEXES_FILE) -> None:
        self.client = client
        self.sync = sync
        self._ensure = ensure
        self._indexes = indexes
        self.touched: Set[str] = set()

    def __call__(self, indexes: Any = None, groups: Optional[Dict[str, str]] = None) -> RealKitDb:
        """`EvalDb`'s signature; `indexes` only switches the fake's refusal, the real database refuses on its own."""
        return RealKitDb(self, groups)

    def touch(self, name: str, groups: Optional[Dict[str, str]]) -> str:
        """The real name of a top-level collection; on its first use in this test its indexes are made READY and it is purged."""
        real = real_name(name, groups)
        if real not in self.touched:
            group = live_group(name, groups)
            wanted = render_indexes(group, self._indexes) if group else []
            if wanted:
                self._ensure(real, wanted)
            self.purge(real)
            self.touched.add(real)
        return real

    def purge(self, real: str) -> None:
        self.sync.recursive_delete(self.sync.collection(real))

    def purge_touched(self) -> None:
        for real in sorted(self.touched):
            self.purge(real)
