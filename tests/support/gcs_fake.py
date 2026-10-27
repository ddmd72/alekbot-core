"""One in-memory fake of the google-cloud-storage surface the GCS adapters use, with GCS semantics (P11 card E).

`FakeGcs` holds every bucket; `client()` is the `storage.Client` surface to inject (`client=` / `adapter._client`);
`install(monkeypatch)` patches `google.cloud.storage.Client` for adapters that build a client per call.

Semantics (each has a self-test in `tests/unit/support/test_gcs_fake.py`; the ones real GCS has not confirmed carry
`# P12 verifies` and are scenarios in `tests/support/gcs_parity.py`):
1. Buckets are keyed by name; generations strictly increase per bucket; metageneration starts at 1, +1 per patch.
2. Versioning per bucket (`versioned=` names): off, an overwrite or delete erases; on, the old live version turns
   noncurrent with `time_deleted`.
3. A failed precondition raises `PreconditionFailed`; `if_generation_match=0` means "no live version" (noncurrent
   versions do not count).
4. A blob built with `generation=` addresses that version: download, patch and exists act on it (noncurrent allowed);
   delete ERASES it (AGENT_WORKSPACE_RFC §5.3 trap). The SDK sends `blob.generation` once a blob is loaded, so a blob
   that was just uploaded or patched is generation-addressed too.
5. `patch` sends only the properties set since the blob was loaded (the SDK's `_changes`): metadata merges, a `None`
   value removes the key, `metadata = None` clears it; it bumps metageneration and `updated`, never the generation;
   `custom_time` cannot move earlier or be removed (`BadRequest`).
6. A missing object: download, delete, patch, reload raise `NotFound`; `get_blob` returns None; `exists` is False (also
   when only noncurrent versions exist).
7. Each request is atomic under one `threading.Lock` (the adapters call the SDK from executor threads). Hooks run
   after the lock is released.
8. `list_blobs`: names in lexicographic order, the versions of a name in generation order, `prefix` + `delimiter`
   (collapsed names in `.prefixes`), the `versions` flag; the results are loaded blobs.
9. Object names: 1..1024 UTF-8 bytes, no CR/LF, not `.`/`..`, else `BadRequest` (checked on upload only: such an
   object cannot exist, so a read of one is `NotFound` here).
10. Times come from the injected clock (default: a fixed instant advancing one second per mutation).

Faults and interleavings, consumed in order per op (`upload patch delete download get list exists`; `get` is
`get_blob` and `reload`): `fail_next(op, exc, landed=False)` raises `exc` before the op (or after it landed, the lost
answer); `on(op, hook)` runs `hook()` once after the op landed, before it returns. Access control is refused: the
buckets are private (`make_public`, `acl`, `predefined_acl` raise AssertionError). Signed URLs are deterministic and
recorded in `signed_urls`. A list keyword this fake does not model raises NotImplementedError: extend the fake then.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple
from urllib.parse import quote

from google.api_core.exceptions import BadRequest, NotFound, PreconditionFailed

__all__ = ["FakeGcs", "FakeGcsClient", "FakeBucket", "FakeBlob", "FakeVersion", "FAKE_EPOCH"]

FAKE_EPOCH = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
_OPS = frozenset({"upload", "patch", "delete", "download", "get", "list", "exists"})
_FIRST_GENERATION = 1001
_UNSET = object()


@dataclass
class FakeVersion:
    """One stored version of one object (inspection returns copies)."""
    name: str
    generation: int
    data: bytes
    content_type: Optional[str]
    metadata: Dict[str, str]
    custom_time: Optional[datetime]
    time_created: datetime
    updated: datetime
    metageneration: int = 1
    time_deleted: Optional[datetime] = None

    @property
    def live(self) -> bool:
        return self.time_deleted is None


@dataclass
class _Bucket:
    versioned: bool
    objects: Dict[str, List[FakeVersion]] = field(default_factory=dict)
    next_generation: int = _FIRST_GENERATION


def _preconditions(version: Optional[FakeVersion], name: str, if_generation_match: Any = None,
                   if_generation_not_match: Any = None, if_metageneration_match: Any = None,
                   if_metageneration_not_match: Any = None) -> None:
    generation = version.generation if version is not None else 0
    failed = (
        (if_generation_match is not None and generation != if_generation_match)
        or (if_generation_not_match is not None and generation == if_generation_not_match)
        or (if_metageneration_match is not None and (version is None or version.metageneration != if_metageneration_match))
        or (if_metageneration_not_match is not None and version is not None
            and version.metageneration == if_metageneration_not_match)
    )
    if failed:
        raise PreconditionFailed(f"412 conditionNotMet {name}")


def _check_name(name: str) -> None:
    if not 1 <= len(name.encode("utf-8")) <= 1024 or "\r" in name or "\n" in name or name in (".", ".."):
        raise BadRequest(f"400 invalid object name {name[:40]!r}")


class FakeGcs:
    """The buckets, the clock, the faults and hooks. One per test."""

    def __init__(self, *, versioned: Iterable[str] = (), clock: Optional[Callable[[], datetime]] = None) -> None:
        self._versioned: Set[str] = set(versioned)
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._now = FAKE_EPOCH
        self._faults: Dict[str, List[Tuple[BaseException, bool]]] = {}
        self._hooks: Dict[str, List[Callable[[], Any]]] = {}
        self.signed_urls: List[Dict[str, Any]] = []

    # -- the SDK surface -----------------------------------------------------------------------------------------

    def client(self) -> "FakeGcsClient":
        return FakeGcsClient(self)

    def install(self, monkeypatch: Any) -> "FakeGcsClient":
        """Patch `google.cloud.storage.Client` so every `storage.Client(...)` returns this fake's client."""
        from google.cloud import storage
        client = self.client()
        monkeypatch.setattr(storage, "Client", lambda *args, **kwargs: client)
        return client

    # -- inspection ----------------------------------------------------------------------------------------------

    def versions(self, bucket: str, name: str) -> List[FakeVersion]:
        """Every stored version of `name`, in generation order (copies)."""
        with self._lock:
            return [replace(v, metadata=dict(v.metadata)) for v in self._bucket(bucket).objects.get(name, [])]

    def live(self, bucket: str, name: str) -> Optional[FakeVersion]:
        return next((v for v in self.versions(bucket, name) if v.live), None)

    def names(self, bucket: str) -> List[str]:
        """Names with a live version, sorted."""
        with self._lock:
            return sorted(n for n, vs in self._bucket(bucket).objects.items() if any(v.live for v in vs))

    # -- faults and interleavings --------------------------------------------------------------------------------

    def fail_next(self, op: str, exc: BaseException, *, landed: bool = False) -> None:
        """The next `op` raises `exc`: before it acts, or (`landed=True`) after it landed (the answer was lost)."""
        self._known(op)
        with self._lock:
            self._faults.setdefault(op, []).append((exc, landed))

    def on(self, op: str, hook: Callable[[], Any]) -> None:
        """`hook()` runs once, after the next `op` landed and before it returns (outside the lock)."""
        self._known(op)
        with self._lock:
            self._hooks.setdefault(op, []).append(hook)

    @staticmethod
    def _known(op: str) -> None:
        if op not in _OPS:
            raise ValueError(f"unknown op {op!r}; one of {sorted(_OPS)}")

    # -- internals (called by the blob/bucket/client classes) ----------------------------------------------------

    def _bucket(self, name: str) -> _Bucket:
        if name not in self._buckets:
            self._buckets[name] = _Bucket(versioned=name in self._versioned)
        return self._buckets[name]

    def _tick(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        self._now += timedelta(seconds=1)
        return self._now

    def _run(self, op: str, body: Callable[[], Any]) -> Any:
        """One request: a planned fault, the body under the lock, then the hook, then a landed fault."""
        with self._lock:
            fault = self._faults[op].pop(0) if self._faults.get(op) else None
            if fault is not None and not fault[1]:
                raise fault[0]
            result = body()
            hook = self._hooks[op].pop(0) if self._hooks.get(op) else None   # only an op that landed runs it
        if hook is not None:
            hook()
        if fault is not None:
            raise fault[0]
        return result

    @staticmethod
    def _live_of(versions: List[FakeVersion]) -> Optional[FakeVersion]:
        return next((v for v in versions if v.time_deleted is None), None)

    def _find(self, bucket: str, name: str, generation: Optional[int]) -> Optional[FakeVersion]:
        versions = self._bucket(bucket).objects.get(name, [])
        if generation is None:
            return self._live_of(versions)
        return next((v for v in versions if v.generation == generation), None)

    def _upload(self, bucket: str, name: str, data: bytes, content_type: Optional[str], metadata: Optional[Dict],
                custom_time: Optional[datetime], preconditions: Dict[str, Any]) -> FakeVersion:
        _check_name(name)
        b = self._bucket(bucket)
        versions = b.objects.setdefault(name, [])
        live = self._live_of(versions)
        _preconditions(live, name, **preconditions)
        now = self._tick()
        if live is not None:
            if b.versioned:
                live.time_deleted = now
            else:
                versions.remove(live)
        version = FakeVersion(name=name, generation=b.next_generation, data=bytes(data), content_type=content_type,
                              metadata={k: v for k, v in (metadata or {}).items() if v is not None},
                              custom_time=custom_time, time_created=now, updated=now)
        b.next_generation += 1
        versions.append(version)
        return version

    def _patch(self, bucket: str, name: str, generation: Optional[int], changes: Dict[str, Any],
               preconditions: Dict[str, Any]) -> FakeVersion:
        version = self._find(bucket, name, generation)   # a noncurrent version may be patched by generation: P12 verifies
        if version is None:
            raise NotFound(f"404 {name}")
        _preconditions(version, name, **preconditions)
        if "custom_time" in changes:
            new = changes["custom_time"]
            if version.custom_time is not None and (new is None or new < version.custom_time):
                raise BadRequest("400 customTime cannot be removed or moved earlier")
        now = self._tick()
        if "metadata" in changes:
            if changes["metadata"] is None:
                version.metadata = {}
            else:
                for k, v in changes["metadata"].items():
                    if v is None:
                        version.metadata.pop(k, None)
                    else:
                        version.metadata[k] = v
        if "custom_time" in changes:
            version.custom_time = changes["custom_time"]
        if "content_type" in changes:
            version.content_type = changes["content_type"]
        version.metageneration += 1
        version.updated = now
        return version

    def _delete(self, bucket: str, name: str, generation: Optional[int], preconditions: Dict[str, Any]) -> None:
        b = self._bucket(bucket)
        versions = b.objects.get(name, [])
        version = self._find(bucket, name, generation)
        if version is None:
            raise NotFound(f"404 {name}")   # a conditional delete of a missing object: 404, not 412 — P12 verifies
        _preconditions(version, name, **preconditions)
        now = self._tick()
        if generation is None and b.versioned:
            version.time_deleted = now
        else:
            versions.remove(version)    # generation-addressed (any bucket) or unversioned: gone for good

    def _list(self, bucket: str, prefix: str, delimiter: Optional[str], versions: bool,
              max_results: Optional[int]) -> Tuple[List[FakeVersion], Set[str]]:
        found: List[FakeVersion] = []
        prefixes: Set[str] = set()
        for name in sorted(self._bucket(bucket).objects):
            if not name.startswith(prefix):
                continue
            if delimiter:
                cut = name.find(delimiter, len(prefix))
                if cut != -1:
                    prefixes.add(name[:cut + len(delimiter)])
                    continue
            # The versions of one name in generation order: P12 verifies.
            found += [v for v in self._bucket(bucket).objects[name] if versions or v.time_deleted is None]
        return (found[:max_results] if max_results is not None else found), prefixes


class FakeGcsClient:
    """`storage.Client` surface: `bucket(name)`, `list_blobs(...)`."""

    def __init__(self, gcs: FakeGcs) -> None:
        self._gcs = gcs

    def bucket(self, name: str, user_project: Any = None) -> "FakeBucket":
        return FakeBucket(self._gcs, name)

    def list_blobs(self, bucket_or_name: Any, max_results: Optional[int] = None, page_token: Any = None,
                   prefix: Optional[str] = None, delimiter: Optional[str] = None, versions: Optional[bool] = None,
                   **kwargs: Any) -> "_Listing":
        unsupported = {k for k, v in kwargs.items() if v is not None and k not in ("projection", "fields", "page_size",
                                                                                    "timeout", "retry")}
        if unsupported or page_token is not None:
            raise NotImplementedError(f"gcs_fake.list_blobs does not model {sorted(unsupported) or ['page_token']}")
        name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        found, prefixes = self._gcs._run("list", lambda: self._gcs._list(name, prefix or "", delimiter, bool(versions),
                                                                        max_results))
        bucket = FakeBucket(self._gcs, name)
        return _Listing([FakeBlob(bucket, v.name, version=v) for v in found], prefixes)


class _Listing:
    """The SDK's HTTPIterator, flattened: iterable once or more; `prefixes` is filled (eagerly here)."""

    def __init__(self, blobs: List["FakeBlob"], prefixes: Set[str]) -> None:
        self._blobs, self.prefixes = blobs, prefixes

    def __iter__(self) -> Iterator["FakeBlob"]:
        return iter(list(self._blobs))


class FakeBucket:
    def __init__(self, gcs: FakeGcs, name: str) -> None:
        self._gcs, self.name = gcs, name

    def blob(self, blob_name: str, chunk_size: Any = None, encryption_key: Any = None, kms_key_name: Any = None,
             generation: Optional[int] = None) -> "FakeBlob":
        return FakeBlob(self, blob_name, generation=generation)

    def get_blob(self, blob_name: str, client: Any = None, encryption_key: Any = None, generation: Optional[int] = None,
                 **preconditions: Any) -> Optional["FakeBlob"]:
        blob = FakeBlob(self, blob_name, generation=generation)
        try:
            blob.reload(**preconditions)
        except NotFound:
            return None
        return blob

    def list_blobs(self, **kwargs: Any) -> _Listing:
        return FakeGcsClient(self._gcs).list_blobs(self.name, **kwargs)


class FakeBlob:
    """`storage.Blob` surface. Writable properties set since the last load are what `patch()` sends."""

    def __init__(self, bucket: FakeBucket, name: str, generation: Optional[int] = None,
                 version: Optional[FakeVersion] = None) -> None:
        self.bucket, self.name = bucket, name
        self._gcs = bucket._gcs
        self.generation: Optional[int] = generation
        self.metageneration: Optional[int] = None
        self.size: Optional[int] = None
        self.time_created: Optional[datetime] = None
        self.updated: Optional[datetime] = None
        self.time_deleted: Optional[datetime] = None
        self._metadata: Optional[Dict[str, Optional[str]]] = None
        self._custom_time: Optional[datetime] = None
        self._content_type: Optional[str] = None
        self._changes: Set[str] = set()
        if version is not None:
            self._load(version)

    # -- writable properties -------------------------------------------------------------------------------------

    @property
    def metadata(self) -> Optional[Dict[str, Optional[str]]]:
        return copy.deepcopy(self._metadata)

    @metadata.setter
    def metadata(self, value: Optional[Dict[str, Any]]) -> None:
        self._metadata = None if value is None else {k: None if v is None else str(v) for k, v in value.items()}
        self._changes.add("metadata")

    @property
    def custom_time(self) -> Optional[datetime]:
        return self._custom_time

    @custom_time.setter
    def custom_time(self, value: Optional[datetime]) -> None:
        self._custom_time = value
        self._changes.add("custom_time")

    @property
    def content_type(self) -> Optional[str]:
        return self._content_type

    @content_type.setter
    def content_type(self, value: Optional[str]) -> None:
        self._content_type = value
        self._changes.add("content_type")

    # -- private bucket ------------------------------------------------------------------------------------------

    @property
    def acl(self) -> Any:
        raise AssertionError("private bucket: no ACL")

    def make_public(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("private bucket: make_public refused")

    # -- requests ------------------------------------------------------------------------------------------------

    def _load(self, v: FakeVersion) -> None:
        self.generation, self.metageneration, self.size = v.generation, v.metageneration, len(v.data)
        self.time_created, self.updated, self.time_deleted = v.time_created, v.updated, v.time_deleted
        self._metadata = dict(v.metadata) if v.metadata else None
        self._custom_time, self._content_type = v.custom_time, v.content_type
        self._changes = set()

    def upload_from_string(self, data: Any, content_type: str = "text/plain", client: Any = None,
                           predefined_acl: Any = None, timeout: Any = None, checksum: Any = None, retry: Any = None,
                           **preconditions: Any) -> None:
        if predefined_acl is not None:
            raise AssertionError("private bucket: predefined_acl refused")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        version = self._gcs._run("upload", lambda: self._snapshot(self._gcs._upload(
            self.bucket.name, self.name, payload, content_type or self._content_type, self._metadata,
            self._custom_time, preconditions)))
        self._load(version)

    def patch(self, client: Any = None, timeout: Any = None, retry: Any = None, override_unlocked_retention: Any = None,
              **preconditions: Any) -> None:
        changes = {k: getattr(self, k) for k in self._changes}
        version = self._gcs._run("patch", lambda: self._snapshot(self._gcs._patch(
            self.bucket.name, self.name, self.generation, changes, preconditions)))
        self._load(version)

    def delete(self, client: Any = None, timeout: Any = None, retry: Any = None, **preconditions: Any) -> None:
        self._gcs._run("delete", lambda: self._gcs._delete(self.bucket.name, self.name, self.generation, preconditions))

    def download_as_bytes(self, client: Any = None, start: Any = None, end: Any = None, raw_download: Any = None,
                          timeout: Any = None, checksum: Any = None, retry: Any = None, **preconditions: Any) -> bytes:
        if start is not None or end is not None:
            raise NotImplementedError("gcs_fake.download_as_bytes does not model ranges")

        def body() -> bytes:
            version = self._gcs._find(self.bucket.name, self.name, self.generation)
            if version is None:
                raise NotFound(f"404 {self.name}")
            _preconditions(version, self.name, **preconditions)
            return version.data

        return self._gcs._run("download", body)

    def exists(self, client: Any = None, timeout: Any = None, retry: Any = None, **preconditions: Any) -> bool:
        def body() -> bool:
            version = self._gcs._find(self.bucket.name, self.name, self.generation)
            if version is None:
                return False
            _preconditions(version, self.name, **preconditions)
            return True

        return self._gcs._run("exists", body)

    def reload(self, client: Any = None, projection: Any = None, timeout: Any = None, retry: Any = None,
               **preconditions: Any) -> None:
        def body() -> FakeVersion:
            version = self._gcs._find(self.bucket.name, self.name, self.generation)
            if version is None:
                raise NotFound(f"404 {self.name}")
            _preconditions(version, self.name, **preconditions)
            return self._snapshot(version)

        self._load(self._gcs._run("get", body))

    def generate_signed_url(self, expiration: Any = None, api_access_endpoint: Any = None, method: str = "GET",
                            version: Optional[str] = None, **kwargs: Any) -> str:
        """Deterministic, recorded, and like the SDK it does not check that the object exists."""
        seconds = int(expiration.total_seconds()) if isinstance(expiration, timedelta) else expiration
        self._gcs.signed_urls.append({"bucket": self.bucket.name, "name": self.name, "expiration": expiration,
                                      "method": method, "version": version, **kwargs})
        return (f"https://storage.googleapis.com/{self.bucket.name}/{quote(self.name)}"
                f"?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Expires={seconds}&X-Goog-Signature=fake")

    @staticmethod
    def _snapshot(version: FakeVersion) -> FakeVersion:
        return replace(version, metadata=dict(version.metadata))
