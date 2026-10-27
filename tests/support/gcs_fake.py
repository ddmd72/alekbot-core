"""One in-memory fake of the google-cloud-storage surface the GCS adapters use, with GCS semantics (P11 card E).

`FakeGcs(buckets={"media-test": "plain", "workspace-test": "versioned"})` holds the declared buckets; `client()` is the
`storage.Client` surface to inject (`client=` / `adapter._client`); `install(monkeypatch)` patches
`google.cloud.storage.Client` for adapters that build a client per call.

Semantics (each has a self-test in `tests/unit/support/test_gcs_fake.py`; the ones real GCS has not confirmed carry
`# P12 verifies` and are scenarios in `tests/support/gcs_parity.py`):
1. Buckets are declared, each "plain" or "versioned"; an operation on an undeclared bucket raises `NotFound`.
   Generations strictly increase per bucket; metageneration starts at 1, +1 per patch.
2. Plain: an overwrite or delete erases. Versioned: the old live version turns noncurrent with `time_deleted`.
3. `if_generation_match` (the only precondition the adapters use) raises `PreconditionFailed` on a mismatch; 0 means
   "no live version" (noncurrent versions do not count).
4. `bucket.blob(name)` is unloaded (generation, metadata, size, content_type None) until `upload_from_string`,
   `patch`, `reload` or `get_blob` loads the response's properties, `generation` included. A blob whose `generation` is
   set, by `blob(name, generation=g)` or by a loaded response, addresses that version: download, patch and exists act
   on it (noncurrent allowed) and delete ERASES it. That is the SDK trap AGENT_WORKSPACE_RFC §5.3 builds the workspace
   adapter around: a delete through the blob that was just patched or uploaded erases the version.
5. `patch` sends only the properties set since the blob was loaded: metadata merges, a `None` value removes the key,
   `metadata = None` clears it; it bumps metageneration and `updated`, never the generation; `custom_time` cannot move
   earlier or be removed (`BadRequest`).
6. A missing object: download, delete, patch, reload raise `NotFound`; `get_blob` returns None; `exists` is False (also
   when only noncurrent versions exist).
7. Each request is atomic under one `threading.Lock` (the adapters call the SDK from executor threads).
8. `list_blobs(bucket, prefix=, versions=)`: names in lexicographic order, the versions of a name in generation order;
   loaded blobs.
9. Object names: 1..1024 UTF-8 bytes, no CR/LF, not `.`/`..`, else `BadRequest` (checked on upload only: such an
   object cannot exist, so a read of one is `NotFound` here).
10. Times come from the injected clock (default: a fixed instant advancing one second per mutation).

Faults and interleavings, consumed in order per op (`upload patch delete download get list exists`; `get` is
`get_blob` and `reload`): `fail_next(op, exc, landed=False)` raises `exc` before the op acts (or after it landed: the
lost answer); `on(op, hook, when="after"|"before")` runs `hook()` once, before the request or after it landed, always
outside the request lock, so a hook may call this fake's SDK surface synchronously.

Refused: any access change (`make_public`, `acl`, `predefined_acl=`: the buckets are private, AssertionError); a V4
signed URL over 7 days (ValueError, as the SDK). Signed URLs are deterministic and their requests recorded in
`signed_urls`. Only the SDK parameters the adapters use exist here, under the SDK's names: a misspelt or unmodelled
parameter is a TypeError, as a misspelt one is on the real SDK; extend the fake (with a parity scenario) to use more.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import quote

from google.api_core.exceptions import BadRequest, NotFound, PreconditionFailed

__all__ = ["FakeGcs", "FakeGcsClient", "FakeBucket", "FakeBlob", "FakeVersion", "FAKE_EPOCH"]

FAKE_EPOCH = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
SEVEN_DAYS = timedelta(days=7)
_OPS = frozenset({"upload", "patch", "delete", "download", "get", "list", "exists"})
_KINDS = {"plain": False, "versioned": True}
_FIRST_GENERATION = 1001


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


def _check_generation(version: Optional[FakeVersion], name: str, if_generation_match: Optional[int]) -> None:
    if if_generation_match is not None and (version.generation if version is not None else 0) != if_generation_match:
        raise PreconditionFailed(f"412 conditionNotMet {name}")


def _check_name(name: str) -> None:
    if not 1 <= len(name.encode("utf-8")) <= 1024 or "\r" in name or "\n" in name or name in (".", ".."):
        raise BadRequest(f"400 invalid object name {name[:40]!r}")


def _snapshot(version: FakeVersion) -> FakeVersion:
    return replace(version, metadata=dict(version.metadata))


class FakeGcs:
    """The declared buckets, the clock, the faults and hooks. One per test."""

    def __init__(self, *, buckets: Dict[str, str], clock: Optional[Callable[[], datetime]] = None) -> None:
        unknown = {kind for kind in buckets.values() if kind not in _KINDS}
        if unknown:
            raise ValueError(f"bucket kind must be 'plain' or 'versioned', got {sorted(unknown)}")
        self._buckets: Dict[str, _Bucket] = {name: _Bucket(versioned=_KINDS[kind]) for name, kind in buckets.items()}
        self._lock = threading.Lock()
        self._clock = clock
        self._now = FAKE_EPOCH
        self._faults: Dict[str, List[Tuple[BaseException, bool]]] = {}
        self._hooks: Dict[Tuple[str, str], List[Callable[[], Any]]] = {}
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
            return [_snapshot(v) for v in self._bucket(bucket).objects.get(name, [])]

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

    def on(self, op: str, hook: Callable[[], Any], *, when: str = "after") -> None:
        """`hook()` runs once at the next `op`: before the request ("before") or after it landed ("after"), outside
        the request lock."""
        self._known(op)
        if when not in ("before", "after"):
            raise ValueError(f"when must be 'before' or 'after', got {when!r}")
        with self._lock:
            self._hooks.setdefault((op, when), []).append(hook)

    @staticmethod
    def _known(op: str) -> None:
        if op not in _OPS:
            raise ValueError(f"unknown op {op!r}; one of {sorted(_OPS)}")

    # -- internals (called by the blob/bucket/client classes) ----------------------------------------------------

    def _bucket(self, name: str) -> _Bucket:
        if name not in self._buckets:
            raise NotFound(f"404 bucket {name} does not exist")
        return self._buckets[name]

    def _tick(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        self._now += timedelta(seconds=1)
        return self._now

    def _pop_hook(self, op: str, when: str) -> Optional[Callable[[], Any]]:
        hooks = self._hooks.get((op, when))
        return hooks.pop(0) if hooks else None

    def _run(self, op: str, body: Callable[[], Any]) -> Any:
        """One request: the before-hook, a planned fault, the body under the lock, the after-hook, a landed fault."""
        with self._lock:
            before = self._pop_hook(op, "before")
        if before is not None:
            before()
        with self._lock:
            fault = self._faults[op].pop(0) if self._faults.get(op) else None
            if fault is not None and not fault[1]:
                raise fault[0]
            result = body()
            after = self._pop_hook(op, "after")      # only a request that landed runs it
        if after is not None:
            after()
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

    def _find_or_404(self, bucket: str, name: str, generation: Optional[int]) -> FakeVersion:
        version = self._find(bucket, name, generation)
        if version is None:
            raise NotFound(f"404 {name}")
        return version

    def _upload(self, bucket: str, name: str, data: bytes, content_type: Optional[str], metadata: Optional[Dict],
                custom_time: Optional[datetime], if_generation_match: Optional[int]) -> FakeVersion:
        b = self._bucket(bucket)
        _check_name(name)
        versions = b.objects.setdefault(name, [])
        live = self._live_of(versions)
        _check_generation(live, name, if_generation_match)
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
        return _snapshot(version)

    def _patch(self, bucket: str, name: str, generation: Optional[int], changes: Dict[str, Any],
               if_generation_match: Optional[int]) -> FakeVersion:
        version = self._find_or_404(bucket, name, generation)   # a noncurrent version by generation: P12 verifies
        _check_generation(version, name, if_generation_match)
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
        return _snapshot(version)

    def _delete(self, bucket: str, name: str, generation: Optional[int], if_generation_match: Optional[int]) -> None:
        b = self._bucket(bucket)
        version = self._find(bucket, name, generation)
        if version is None:
            raise NotFound(f"404 {name}")   # a conditional delete of a missing object: 404, not 412 — P12 verifies
        _check_generation(version, name, if_generation_match)
        now = self._tick()
        if generation is None and b.versioned:
            version.time_deleted = now
        else:
            b.objects[name].remove(version)   # generation-addressed (any bucket) or plain: gone for good

    def _list(self, bucket: str, prefix: str, versions: bool) -> List[FakeVersion]:
        b = self._bucket(bucket)
        found: List[FakeVersion] = []
        for name in sorted(b.objects):
            if name.startswith(prefix):
                # The versions of one name in generation order: P12 verifies.
                found += [_snapshot(v) for v in b.objects[name] if versions or v.time_deleted is None]
        return found


class FakeGcsClient:
    """`storage.Client` surface: `bucket(bucket_name)`, `list_blobs(bucket_or_name, prefix=, versions=)`."""

    def __init__(self, gcs: FakeGcs) -> None:
        self._gcs = gcs

    def bucket(self, bucket_name: str) -> "FakeBucket":
        return FakeBucket(self._gcs, bucket_name)

    def list_blobs(self, bucket_or_name: Any, prefix: Optional[str] = None,
                   versions: Optional[bool] = None) -> Iterator["FakeBlob"]:
        name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        found = self._gcs._run("list", lambda: self._gcs._list(name, prefix or "", bool(versions)))
        bucket = FakeBucket(self._gcs, name)
        return iter([FakeBlob(bucket, v.name, version=v) for v in found])


class FakeBucket:
    def __init__(self, gcs: FakeGcs, name: str) -> None:
        self._gcs, self.name = gcs, name

    def blob(self, blob_name: str, generation: Optional[int] = None) -> "FakeBlob":
        return FakeBlob(self, blob_name, generation=generation)

    def get_blob(self, blob_name: str, generation: Optional[int] = None) -> Optional["FakeBlob"]:
        blob = FakeBlob(self, blob_name, generation=generation)
        try:
            blob.reload()
        except NotFound:
            return None
        return blob

    def list_blobs(self, prefix: Optional[str] = None, versions: Optional[bool] = None) -> Iterator["FakeBlob"]:
        return FakeGcsClient(self._gcs).list_blobs(self.name, prefix=prefix, versions=versions)


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

    # -- private buckets -----------------------------------------------------------------------------------------

    @property
    def acl(self) -> Any:
        raise AssertionError("private bucket: no ACL")

    def make_public(self) -> None:
        raise AssertionError("private bucket: make_public refused")

    # -- requests ------------------------------------------------------------------------------------------------

    def _load(self, v: FakeVersion) -> None:
        self.generation, self.metageneration, self.size = v.generation, v.metageneration, len(v.data)
        self.time_created, self.updated, self.time_deleted = v.time_created, v.updated, v.time_deleted
        self._metadata = dict(v.metadata) if v.metadata else None
        self._custom_time, self._content_type = v.custom_time, v.content_type
        self._changes = set()

    def upload_from_string(self, data: Any, content_type: str = "text/plain", predefined_acl: Optional[str] = None,
                           if_generation_match: Optional[int] = None) -> None:
        if predefined_acl is not None:
            raise AssertionError("private bucket: predefined_acl refused")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._load(self._gcs._run("upload", lambda: self._gcs._upload(
            self.bucket.name, self.name, payload, content_type, self._metadata, self._custom_time,
            if_generation_match)))

    def patch(self, if_generation_match: Optional[int] = None) -> None:
        changes = {k: getattr(self, k) for k in self._changes}
        self._load(self._gcs._run("patch", lambda: self._gcs._patch(
            self.bucket.name, self.name, self.generation, changes, if_generation_match)))

    def delete(self, if_generation_match: Optional[int] = None) -> None:
        self._gcs._run("delete", lambda: self._gcs._delete(self.bucket.name, self.name, self.generation,
                                                           if_generation_match))

    def download_as_bytes(self) -> bytes:
        return self._gcs._run("download",
                              lambda: self._gcs._find_or_404(self.bucket.name, self.name, self.generation).data)

    def exists(self) -> bool:
        return self._gcs._run("exists",
                              lambda: self._gcs._find(self.bucket.name, self.name, self.generation) is not None)

    def reload(self) -> None:
        self._load(self._gcs._run("get", lambda: _snapshot(
            self._gcs._find_or_404(self.bucket.name, self.name, self.generation))))

    def generate_signed_url(self, expiration: Any = None, method: str = "GET", version: Optional[str] = None,
                            service_account_email: Optional[str] = None, access_token: Optional[str] = None) -> str:
        """Deterministic and recorded; like the SDK it does not check that the object exists, and refuses a V4 URL
        valid over 7 days."""
        if not isinstance(expiration, timedelta):
            raise NotImplementedError("gcs_fake.generate_signed_url models a timedelta expiration only")
        if version == "v4" and expiration > SEVEN_DAYS:
            raise ValueError("Max allowed expiration interval is seven days 604800")
        self._gcs.signed_urls.append({"bucket": self.bucket.name, "name": self.name, "expiration": expiration,
                                      "method": method, "version": version,
                                      "service_account_email": service_account_email, "access_token": access_token})
        return (f"https://storage.googleapis.com/{self.bucket.name}/{quote(self.name)}"
                f"?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Expires={int(expiration.total_seconds())}"
                f"&X-Goog-Signature=fake")
