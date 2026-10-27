"""Fake-vs-real parity scenarios for GCS (P11 card E): one set of scenarios, two backends.

Each scenario is `def scenario(client, bucket, prefix) -> result`: it writes objects under `prefix` in `bucket` through
the `storage.Client` surface (`client` is the real client or `tests.support.gcs_fake.FakeGcs().client()`) and returns
ONE observable value. `SCENARIOS` pairs every function with its expected value: GCS's documented behaviour, or, where
the documentation is silent, the value the fake answers today with `p12=True`: the cloud run (P12, the versioned test
bucket) settles those three (version list order, a conditional delete of a missing object, a metadata patch of a
noncurrent version) and the error class of an over-long name.

Backends: the fake (`tests/unit/support/test_gcs_parity.py`, in `make check`) and the test bucket
(`tests/cloud/test_gcs_parity_cloud.py`, `make test-cloud`). The bucket is versioned, so every scenario assumes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List

from google.api_core import exceptions as gexc

__all__ = ["Scenario", "SCENARIOS", "purge"]

T0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


def _outcome(fn: Callable[[], Any]) -> str:
    """"ok" when the call succeeds, else the class name of the error the backend raised."""
    try:
        fn()
    except gexc.GoogleAPICallError as exc:
        return type(exc).__name__
    return "ok"


def _put(client: Any, bucket: str, name: str, data: bytes, **kwargs: Any) -> Any:
    blob = client.bucket(bucket).blob(name)
    blob.upload_from_string(data, content_type="text/plain", **kwargs)
    return blob


def _versions(client: Any, bucket: str, name: str) -> List[Any]:
    return [b for b in client.list_blobs(bucket, prefix=name, versions=True) if b.name == name]


def purge(client: Any, bucket: str, prefix: str) -> None:
    """Erase every version under `prefix` (generation-addressed deletes)."""
    for b in list(client.list_blobs(bucket, prefix=prefix, versions=True)):
        try:
            client.bucket(bucket).blob(b.name, generation=b.generation).delete()
        except gexc.NotFound:
            pass


# ------------------------------------------------------------------------------------------- create-if-absent

def create_if_absent_on_a_missing_name(client: Any, bucket: str, p: str) -> str:
    return _outcome(lambda: _put(client, bucket, f"{p}a.md", b"v1", if_generation_match=0))


def create_if_absent_on_a_live_name(client: Any, bucket: str, p: str) -> str:
    _put(client, bucket, f"{p}a.md", b"v1")
    return _outcome(lambda: _put(client, bucket, f"{p}a.md", b"v2", if_generation_match=0))


def create_if_absent_when_only_noncurrent_versions_exist(client: Any, bucket: str, p: str) -> str:
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    client.bucket(bucket).blob(f"{p}a.md").delete(if_generation_match=blob.generation)
    return _outcome(lambda: _put(client, bucket, f"{p}a.md", b"v2", if_generation_match=0))


# ---------------------------------------------------------------------------------------- versions on overwrite

def overwrite_makes_the_old_version_noncurrent(client: Any, bucket: str, p: str) -> Any:
    first = _put(client, bucket, f"{p}a.md", b"v1")
    _put(client, bucket, f"{p}a.md", b"v2", if_generation_match=first.generation)
    by_generation = sorted(_versions(client, bucket, f"{p}a.md"), key=lambda b: int(b.generation))
    return [b.time_deleted is not None for b in by_generation]


def list_order_of_the_versions_of_one_name(client: Any, bucket: str, p: str) -> Any:
    for data in (b"v1", b"v2", b"v3"):
        _put(client, bucket, f"{p}a.md", data)
    return [client.bucket(bucket).blob(b.name, generation=b.generation).download_as_bytes()
            for b in _versions(client, bucket, f"{p}a.md")]


def get_blob_of_a_noncurrent_generation(client: Any, bucket: str, p: str) -> Any:
    first = _put(client, bucket, f"{p}a.md", b"v1")
    _put(client, bucket, f"{p}a.md", b"v2")
    old = client.bucket(bucket).get_blob(f"{p}a.md", generation=first.generation)
    return old is not None and old.time_deleted is not None


# ------------------------------------------------------------------------------------------------------- patch

def patch_merges_removes_and_bumps_the_metageneration(client: Any, bucket: str, p: str) -> Any:
    blob = client.bucket(bucket).blob(f"{p}a.md")
    blob.metadata = {"origin": "chat", "fire": "x"}
    blob.upload_from_string(b"v1", content_type="text/plain")
    generation, metageneration = blob.generation, blob.metageneration
    patcher = client.bucket(bucket).blob(f"{p}a.md")
    patcher.metadata = {"fire": None, "deleted_by": "chat"}
    patcher.patch(if_generation_match=generation)
    after = client.bucket(bucket).get_blob(f"{p}a.md")
    return after.metadata, after.metageneration - metageneration, after.generation == generation


def generation_precondition_still_holds_after_a_patch(client: Any, bucket: str, p: str) -> str:
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    patcher = client.bucket(bucket).blob(f"{p}a.md")
    patcher.metadata = {"deleted_by": "chat"}
    patcher.patch(if_generation_match=blob.generation)
    return _outcome(lambda: _put(client, bucket, f"{p}a.md", b"v2", if_generation_match=blob.generation))


def patch_of_a_noncurrent_version_by_generation(client: Any, bucket: str, p: str) -> Any:
    first = _put(client, bucket, f"{p}a.md", b"v1")
    _put(client, bucket, f"{p}a.md", b"v2")
    old = client.bucket(bucket).blob(f"{p}a.md", generation=first.generation)
    old.metadata = {"deleted_by": None, "note": "kept"}
    old.patch()
    return client.bucket(bucket).get_blob(f"{p}a.md", generation=first.generation).metadata


def custom_time_moved_earlier_is_refused(client: Any, bucket: str, p: str) -> str:
    blob = client.bucket(bucket).blob(f"{p}scratch.md")
    blob.custom_time = T0
    blob.upload_from_string(b"v1", content_type="text/plain")
    patcher = client.bucket(bucket).blob(f"{p}scratch.md")
    patcher.custom_time = T0 - timedelta(days=1)
    return _outcome(patcher.patch)


# ------------------------------------------------------------------------------------------------------ delete

def conditional_delete_of_live_keeps_a_readable_noncurrent_version(client: Any, bucket: str, p: str) -> Any:
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    client.bucket(bucket).blob(f"{p}a.md").delete(if_generation_match=blob.generation)
    live = client.bucket(bucket).get_blob(f"{p}a.md")
    return live is None, client.bucket(bucket).blob(f"{p}a.md", generation=blob.generation).download_as_bytes()


def generation_addressed_delete_erases_that_version(client: Any, bucket: str, p: str) -> Any:
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    client.bucket(bucket).blob(f"{p}a.md", generation=blob.generation).delete()
    return client.bucket(bucket).get_blob(f"{p}a.md", generation=blob.generation) is None, \
        len(_versions(client, bucket, f"{p}a.md"))


def delete_after_patch_through_the_same_blob_erases_the_version(client: Any, bucket: str, p: str) -> Any:
    """The SDK loads the generation from the patch response and sends it with the delete (AGENT_WORKSPACE_RFC §5.3)."""
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    patched = client.bucket(bucket).blob(f"{p}a.md")
    patched.metadata = {"deleted_by": "chat"}
    patched.patch(if_generation_match=blob.generation)
    patched.delete(if_generation_match=blob.generation)
    return client.bucket(bucket).get_blob(f"{p}a.md", generation=blob.generation) is None, \
        len(_versions(client, bucket, f"{p}a.md"))


def conditional_delete_of_a_missing_object(client: Any, bucket: str, p: str) -> str:
    return _outcome(lambda: client.bucket(bucket).blob(f"{p}never.md").delete(if_generation_match=5))


def stale_conditional_delete_is_refused_and_leaves_the_object(client: Any, bucket: str, p: str) -> Any:
    first = _put(client, bucket, f"{p}a.md", b"v1")
    _put(client, bucket, f"{p}a.md", b"v2")
    outcome = _outcome(lambda: client.bucket(bucket).blob(f"{p}a.md").delete(if_generation_match=first.generation))
    return outcome, client.bucket(bucket).blob(f"{p}a.md").download_as_bytes()


# --------------------------------------------------------------------------------------------- reads and lists

def exists_when_only_noncurrent_versions_exist(client: Any, bucket: str, p: str) -> bool:
    blob = _put(client, bucket, f"{p}a.md", b"v1")
    client.bucket(bucket).blob(f"{p}a.md").delete(if_generation_match=blob.generation)
    return bool(client.bucket(bucket).blob(f"{p}a.md").exists())


def prefix_listing_includes_longer_names(client: Any, bucket: str, p: str) -> Any:
    for name in ("a.md", "a.md.bak", "b.md"):
        _put(client, bucket, f"{p}{name}", b"x")
    return [b.name[len(p):] for b in client.list_blobs(bucket, prefix=f"{p}a.md")]


def name_over_1024_bytes_is_refused(client: Any, bucket: str, p: str) -> str:
    name = p + "x" * (1025 - len(p.encode("utf-8")))
    return _outcome(lambda: _put(client, bucket, name, b"x"))


# ------------------------------------------------------------------------------------------------- the table

@dataclass(frozen=True)
class Scenario:
    run: Callable[[Any, str, str], Any]
    expected: Any
    p12: bool = False          # the documentation is silent: the cloud run settles the value

    @property
    def name(self) -> str:
        return self.run.__name__


SCENARIOS = [
    Scenario(create_if_absent_on_a_missing_name, "ok"),
    Scenario(create_if_absent_on_a_live_name, "PreconditionFailed"),
    Scenario(create_if_absent_when_only_noncurrent_versions_exist, "ok"),
    Scenario(overwrite_makes_the_old_version_noncurrent, [True, False]),
    Scenario(list_order_of_the_versions_of_one_name, [b"v1", b"v2", b"v3"], p12=True),
    Scenario(get_blob_of_a_noncurrent_generation, True),
    Scenario(patch_merges_removes_and_bumps_the_metageneration, ({"origin": "chat", "deleted_by": "chat"}, 1, True)),
    Scenario(generation_precondition_still_holds_after_a_patch, "ok"),
    Scenario(patch_of_a_noncurrent_version_by_generation, {"note": "kept"}, p12=True),
    Scenario(custom_time_moved_earlier_is_refused, "BadRequest"),
    Scenario(conditional_delete_of_live_keeps_a_readable_noncurrent_version, (True, b"v1")),
    Scenario(generation_addressed_delete_erases_that_version, (True, 0)),
    Scenario(delete_after_patch_through_the_same_blob_erases_the_version, (True, 0)),
    Scenario(conditional_delete_of_a_missing_object, "NotFound", p12=True),
    Scenario(stale_conditional_delete_is_refused_and_leaves_the_object, ("PreconditionFailed", b"v2")),
    Scenario(exists_when_only_noncurrent_versions_exist, False),
    Scenario(prefix_listing_includes_longer_names, ["a.md", "a.md.bak"]),
    Scenario(name_over_1024_bytes_is_refused, "BadRequest", p12=True),
]
