"""The backend seam of the GCS store kits (TEST_COVERAGE_SYSTEMIC_RFC P12), the GCS twin of `tests/support/kit_backend.py`.

A GCS kit builds its fake over `kit_gcs(buckets={...})`, the factory this fixture gives, with `FakeGcs`'s arguments:

* in the unit suite (`tests/conftest.py` imports the fixture below) `kit_gcs` is `FakeGcs` itself: offline, unchanged;
* in the cloud gate the same kit module is collected again by `tests/cloud/test_kits_cloud.py`, and `tests/cloud/conftest.py`
  overrides `kit_gcs` with a `RealKitGcsBackend`: the call returns a `RealGcs`, FakeGcs's surface over the versioned test
  bucket (`TEST_GCS_BUCKET`) behind the guards of that conftest.

**Names.** Every bucket the kit declares lives in the one test bucket, under `kits/<run id>/<test number>/<declared name>/`;
the client a kit gets (`client()`, `install(monkeypatch)`) adds that prefix to every object name it sends and takes it off
every name it returns, so the adapter sees the names it wrote. The test's prefix is erased (every version) before its first
use and after the test; the bucket's 1-day lifecycle catches a crashed run. An undeclared bucket name is `NotFound`, as on
the fake. The client refuses an access change (`make_public`, `acl`, `predefined_acl=`) with `AssertionError`, as the fake
does: nothing in the test bucket is ever made public.

**A plain bucket on a versioned one.** The test bucket is versioned; a bucket the kit declares "plain" is read through the
inspection helpers (`versions`, `live`, `names`) as its live versions only. That is exact for every adapter that never
addresses a generation (the media and file-storage adapters): a plain bucket keeps after any upload or delete exactly the
versions that are live in a versioned one, and every precondition, read and `exists` looks at the live version only on both.
The adapters' own requests are never filtered.

**Faults and interleavings run here too.** `fail_next(op, exc, landed=)` and `on(op, hook, when=)` have the fake's meaning,
applied at the same SDK calls (`upload` = `upload_from_string`, `patch`, `delete`, `download` = `download_as_bytes`, `get` =
`get_blob` and `reload`, `list` = `list_blobs`, `exists`), around the real request: a planned fault raises before the
request is sent (or after it landed), a hook runs before it is sent or after it landed and may call the real client. A fault
or hook the test planned and the adapter never reached fails the test at teardown ("never reached on the real backend").
The inspection helpers read the bucket directly and never consume a fault or hook.

**Fake-only.** `signed_urls` (the fake's record of signing requests: real signing needs a real credential, so a test that
pins the request with a stand-in credential cannot run here) and `FakeGcs(clock=...)` raise. Such a test carries
`@pytest.mark.gcs_fake_only("<why>")` (checked by `tests/unit/support/test_gcs_fake_only_marks.py`) and is skipped in the
cloud run; a test that fails on the real bucket is never marked: the fake is fixed, or the adapter defect is a strict
xfail `P12-D<n>`.
"""
from __future__ import annotations

import itertools
import threading
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from google.api_core.exceptions import NotFound

from tests.support.gcs_fake import _KINDS, _OPS, FakeGcs, FakeVersion
from tests.support.gcs_parity import purge

__all__ = ["kit_gcs", "RealKitGcsBackend", "RealGcs", "KIT_ROOT"]

KIT_ROOT = "kits/"


@pytest.fixture
def kit_gcs() -> Callable[..., Any]:
    """The GCS kits' backend factory: `FakeGcs` in the unit suite (overridden in `tests/cloud/conftest.py`)."""
    return FakeGcs


class RealKitGcsBackend:
    """The cloud `kit_gcs`: a factory of one `RealGcs` per test over the guarded storage client and the test bucket.
    `prefix` is this test's root (`kits/<run id>/<n>/`)."""

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix
        self._gcs: Optional[RealGcs] = None

    def __call__(self, *, buckets: Dict[str, str], clock: Any = None) -> "RealGcs":
        if clock is not None:
            raise ValueError("kit_gcs(clock=...) cannot be honoured on the real bucket (the server sets the times): "
                             "mark the test gcs_fake_only")
        if self._gcs is not None:      # two fakes are independent; two real ones would share the test's prefix
            raise ValueError("one kit_gcs per test on the real backend: share it, or mark the test gcs_fake_only")
        self.purge()
        self._gcs = RealGcs(self.client, self.bucket, self.prefix, buckets)
        return self._gcs

    def purge(self) -> None:
        purge(self.client, self.bucket, self.prefix)

    def teardown(self) -> None:
        """The test's teardown: erase its prefix, then fail if a fault or hook it planned was never reached."""
        self.purge()
        if self._gcs is not None:
            self._gcs.check_reached()


def run_prefix(run_id: Optional[str] = None) -> Iterator[str]:
    """`kits/<run id>/0/`, `kits/<run id>/1/`, ...: one fresh prefix per test of a run."""
    run_id = run_id or uuid.uuid4().hex[:8]
    return (f"{KIT_ROOT}{run_id}/{n}/" for n in itertools.count())


class RealGcs:
    """FakeGcs's surface over the real test bucket (module docstring)."""

    def __init__(self, client: Any, bucket: str, prefix: str, buckets: Dict[str, str]) -> None:
        unknown = {kind for kind in buckets.values() if kind not in _KINDS}
        if unknown:
            raise ValueError(f"bucket kind must be 'plain' or 'versioned', got {sorted(unknown)}")
        self._real = client
        self._bucket_name = bucket
        self._prefix = prefix
        self._declared = dict(buckets)
        self._lock = threading.Lock()
        self._faults: Dict[str, List[Tuple[BaseException, bool]]] = {}
        self._hooks: Dict[Tuple[str, str], List[Callable[[], Any]]] = {}

    # -- the SDK surface -----------------------------------------------------------------------------------------

    def client(self) -> "PrefixedClient":
        return PrefixedClient(self)

    def install(self, monkeypatch: Any) -> "PrefixedClient":
        """Patch `google.cloud.storage.Client` so every `storage.Client(...)` returns this backend's client."""
        from google.cloud import storage
        client = self.client()
        monkeypatch.setattr(storage, "Client", lambda *args, **kwargs: client)
        return client

    # -- inspection (direct reads: never a fault or hook) -------------------------------------------------------

    def versions(self, bucket: str, name: str) -> List[FakeVersion]:
        """Every stored version of `name` in generation order; a "plain" bucket's live version only (module docstring)."""
        full = self.real_name(bucket, name)
        found = sorted((b for b in self._real.list_blobs(self._bucket_name, prefix=full, versions=True) if b.name == full),
                       key=lambda b: int(b.generation))
        if not _KINDS[self._declared[bucket]]:
            found = [b for b in found if b.time_deleted is None]
        return [self._version(name, b) for b in found]

    def live(self, bucket: str, name: str) -> Optional[FakeVersion]:
        return next((v for v in self.versions(bucket, name) if v.live), None)

    def names(self, bucket: str) -> List[str]:
        """Names with a live version, sorted."""
        root = self.real_name(bucket, "")
        return sorted(b.name[len(root):] for b in self._real.list_blobs(self._bucket_name, prefix=root))

    def _version(self, name: str, blob: Any) -> FakeVersion:
        data = self._real.bucket(self._bucket_name).blob(blob.name, generation=blob.generation).download_as_bytes()
        return FakeVersion(name=name, generation=int(blob.generation), data=data, content_type=blob.content_type,
                           metadata=dict(blob.metadata or {}), custom_time=blob.custom_time,
                           time_created=blob.time_created, updated=blob.updated,
                           metageneration=int(blob.metageneration), time_deleted=blob.time_deleted)

    # -- faults and interleavings (the fake's meaning, around the real request) ----------------------------------

    def fail_next(self, op: str, exc: BaseException, *, landed: bool = False) -> None:
        self._known(op)
        with self._lock:
            self._faults.setdefault(op, []).append((exc, landed))

    def on(self, op: str, hook: Callable[[], Any], *, when: str = "after") -> None:
        self._known(op)
        if when not in ("before", "after"):
            raise ValueError(f"when must be 'before' or 'after', got {when!r}")
        with self._lock:
            self._hooks.setdefault((op, when), []).append(hook)

    @staticmethod
    def _known(op: str) -> None:
        if op not in _OPS:
            raise ValueError(f"unknown op {op!r}; one of {sorted(_OPS)}")

    def run(self, op: str, body: Callable[[], Any]) -> Any:
        """One request: the before-hook, a planned fault, the real request, the after-hook, a landed fault (FakeGcs._run)."""
        with self._lock:
            hooks = self._hooks.get((op, "before"))
            before = hooks.pop(0) if hooks else None
        if before is not None:
            before()
        with self._lock:
            fault = self._faults[op].pop(0) if self._faults.get(op) else None
        if fault is not None and not fault[1]:
            raise fault[0]
        result = body()
        with self._lock:
            hooks = self._hooks.get((op, "after"))
            after = hooks.pop(0) if hooks else None
        if after is not None:
            after()
        if fault is not None:
            raise fault[0]
        return result

    def check_reached(self) -> None:
        left = [f"fail_next({op!r})" for op, faults in self._faults.items() for _ in faults] + \
               [f"on({op!r}, when={when!r})" for (op, when), hooks in self._hooks.items() for _ in hooks]
        if left:
            pytest.fail(f"planned but never reached on the real backend: {', '.join(sorted(left))} (the adapter never made "
                        "that request here: plan it where the adapter calls, or mark the test gcs_fake_only)")

    # -- fake-only -------------------------------------------------------------------------------------------------

    @property
    def signed_urls(self) -> Any:
        raise AttributeError("RealGcs.signed_urls: the fake's record of signing requests has no real counterpart; "
                             "mark the test gcs_fake_only")

    # -- names -----------------------------------------------------------------------------------------------------

    def real_name(self, bucket: str, name: str) -> str:
        if bucket not in self._declared:
            raise NotFound(f"404 bucket {bucket} does not exist")
        return f"{self._prefix}{bucket}/{name}"

    def real_bucket(self) -> Any:
        return self._real.bucket(self._bucket_name)

    @property
    def real_client(self) -> Any:
        return self._real


class PrefixedClient:
    """`storage.Client` surface: `bucket(bucket_name)`, `list_blobs(bucket_or_name, prefix=, versions=)`."""

    def __init__(self, gcs: RealGcs) -> None:
        self._gcs = gcs

    def bucket(self, bucket_name: str) -> "PrefixedBucket":
        return PrefixedBucket(self._gcs, bucket_name)

    def list_blobs(self, bucket_or_name: Any, *, prefix: Optional[str] = None,
                   versions: Optional[bool] = None) -> Iterator["PrefixedBlob"]:
        name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        root = self._gcs.real_name(name, "")
        bucket = PrefixedBucket(self._gcs, name)
        found = self._gcs.run("list", lambda: list(self._gcs.real_client.list_blobs(
            self._gcs.real_bucket().name, prefix=root + (prefix or ""), versions=bool(versions))))
        return iter([PrefixedBlob(bucket, b) for b in found])


class PrefixedBucket:
    def __init__(self, gcs: RealGcs, name: str) -> None:
        self._gcs, self.name = gcs, name

    def blob(self, blob_name: str, *, generation: Optional[int] = None) -> "PrefixedBlob":
        inner = self._gcs.real_bucket().blob(self._gcs.real_name(self.name, blob_name), generation=generation)
        return PrefixedBlob(self, inner)

    def get_blob(self, blob_name: str, *, generation: Optional[int] = None) -> Optional["PrefixedBlob"]:
        full = self._gcs.real_name(self.name, blob_name)
        inner = self._gcs.run("get", lambda: self._gcs.real_bucket().get_blob(full, generation=generation))
        return None if inner is None else PrefixedBlob(self, inner)

    def list_blobs(self, *, prefix: Optional[str] = None, versions: Optional[bool] = None) -> Iterator["PrefixedBlob"]:
        return PrefixedClient(self._gcs).list_blobs(self.name, prefix=prefix, versions=versions)


class PrefixedBlob:
    """A real `storage.Blob` whose `name` is the kit's name (the prefix taken off); every request goes through `RealGcs.run`.
    Reading or setting a property is the real blob's (a read-only property raises there, as in the SDK). A method of the
    real blob is reachable only when this class wraps it (the requests below) or it is in `PASSED_METHODS`: any other one
    (`upload_from_file`, `compose`, `rewrite`, ...) would skip the access-change refusal and the fault and hook bookkeeping,
    so it raises, naming the method (wrap it here, with its op, to use it)."""

    _OWN = frozenset({"_bucket", "_inner"})
    # Methods passed to the real blob unwrapped, each with why it is safe: it sends no request that reads or writes an object.
    PASSED_METHODS = frozenset({
        "generate_signed_url",      # signs locally or through IAM signBlob; never touches the object or its ACL
    })

    def __init__(self, bucket: PrefixedBucket, inner: Any) -> None:
        object.__setattr__(self, "_bucket", bucket)
        object.__setattr__(self, "_inner", inner)

    @property
    def name(self) -> str:
        root = self._bucket._gcs.real_name(self._bucket.name, "")
        return str(self._inner.name)[len(root):]

    @property
    def bucket(self) -> PrefixedBucket:
        return self._bucket

    def __getattr__(self, attr: str) -> Any:
        found = getattr(self._inner, attr)
        if callable(found) and attr not in self.PASSED_METHODS:
            raise AttributeError(f"PrefixedBlob.{attr}: a method the GCS kit backend does not wrap (it would skip the "
                                 "access-change refusal and the fault and hook bookkeeping); wrap it in "
                                 "tests/support/gcs_kit_backend.py with its op")
        return found

    def __setattr__(self, attr: str, value: Any) -> None:
        if attr in self._OWN:
            object.__setattr__(self, attr, value)
        else:
            setattr(self._inner, attr, value)

    # -- private bucket ------------------------------------------------------------------------------------------

    @property
    def acl(self) -> Any:
        raise AssertionError("private bucket: no ACL")

    def make_public(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("private bucket: make_public refused")

    # -- requests ------------------------------------------------------------------------------------------------

    def _run(self, op: str, body: Callable[[], Any]) -> Any:
        return self._bucket._gcs.run(op, body)

    def upload_from_string(self, data: Any, content_type: str = "text/plain", **kwargs: Any) -> None:
        if kwargs.get("predefined_acl") is not None:
            raise AssertionError("private bucket: predefined_acl refused")
        self._run("upload", lambda: self._inner.upload_from_string(data, content_type=content_type, **kwargs))

    def patch(self, **kwargs: Any) -> None:
        self._run("patch", lambda: self._inner.patch(**kwargs))

    def delete(self, **kwargs: Any) -> None:
        self._run("delete", lambda: self._inner.delete(**kwargs))

    def download_as_bytes(self, **kwargs: Any) -> bytes:
        return self._run("download", lambda: self._inner.download_as_bytes(**kwargs))

    def exists(self, **kwargs: Any) -> bool:
        return self._run("exists", lambda: self._inner.exists(**kwargs))

    def reload(self, **kwargs: Any) -> None:
        self._run("get", lambda: self._inner.reload(**kwargs))
