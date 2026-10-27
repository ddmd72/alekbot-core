"""Self-tests of the shared GCS fake (tests/support/gcs_fake.py): one test per semantic of its module docstring, plus
faults, hooks, the refusals, signed URLs and the install helper. What real GCS settles is in the parity scenarios
(tests/support/gcs_parity.py)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import BadRequest, NotFound, PreconditionFailed, ServiceUnavailable

from tests.support.gcs_fake import FAKE_EPOCH, FakeGcs

V, U = "ws-test", "media-test"       # a versioned and a plain bucket
NAME = "u1/projects/a.md"


@pytest.fixture
def gcs():
    return FakeGcs(buckets={V: "versioned", U: "plain"})


def put(gcs, bucket, data, name=NAME, **kw):
    blob = gcs.client().bucket(bucket).blob(name)
    blob.upload_from_string(data, **kw)
    return blob


# 1 ----------------------------------------------------------------------------------- buckets and generations

def test_generations_increase_per_bucket_and_metageneration_starts_at_one(gcs):
    a, b = put(gcs, V, b"v1"), put(gcs, V, b"v2")
    other = put(gcs, U, b"x")
    assert b.generation > a.generation
    assert other.generation == a.generation         # per bucket: each bucket counts on its own
    assert (a.metageneration, b.metageneration) == (1, 1)


def test_an_undeclared_bucket_is_not_found(gcs):
    bucket = gcs.client().bucket("other-test")
    blob = bucket.blob(NAME)
    for call in (lambda: blob.upload_from_string(b"x"), blob.download_as_bytes, blob.exists, blob.delete,
                 blob.patch, blob.reload, lambda: list(bucket.list_blobs())):
        with pytest.raises(NotFound):
            call()
    assert bucket.get_blob(NAME) is None            # the SDK's get_blob turns any NotFound into None
    with pytest.raises(ValueError):
        FakeGcs(buckets={U: "archive"})


# 2 ----------------------------------------------------------------------------------------------- versioning

def test_versioned_overwrite_keeps_a_noncurrent_version(gcs):
    a = put(gcs, V, b"v1")
    b = put(gcs, V, b"v2")
    old, new = gcs.versions(V, NAME)
    assert (old.generation, old.data, old.live, new.generation, new.live) == (a.generation, b"v1", False, b.generation, True)
    assert old.time_deleted == new.time_created


def test_plain_overwrite_and_delete_erase(gcs):
    put(gcs, U, b"v1")
    put(gcs, U, b"v2")
    assert [v.data for v in gcs.versions(U, NAME)] == [b"v2"]
    gcs.client().bucket(U).blob(NAME).delete()
    assert gcs.versions(U, NAME) == []


def test_versioned_delete_of_the_live_object_keeps_it_noncurrent(gcs):
    a = put(gcs, V, b"v1")
    gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    [v] = gcs.versions(V, NAME)
    assert v.time_deleted is not None and gcs.live(V, NAME) is None


# 3 --------------------------------------------------------------------------------------------- preconditions

def test_generation_match_zero_means_no_live_version(gcs):
    a = put(gcs, V, b"v1", if_generation_match=0)
    with pytest.raises(PreconditionFailed):
        put(gcs, V, b"v2", if_generation_match=0)
    with pytest.raises(PreconditionFailed):
        put(gcs, V, b"v2", if_generation_match=a.generation + 1)
    gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    put(gcs, V, b"v3", if_generation_match=0)       # only noncurrent versions: they do not count
    assert gcs.live(V, NAME).data == b"v3"


def test_unmodelled_or_misspelt_parameters_are_type_errors(gcs):
    blob = put(gcs, V, b"v1")
    for call in (lambda: blob.upload_from_string(b"x", if_generation_matchh=0),
                 lambda: blob.delete(if_metageneration_match=1),
                 lambda: blob.download_as_bytes(if_generation_match=1),
                 lambda: gcs.client().list_blobs(V, delimiter="/"),
                 lambda: gcs.client().list_blobs(V, max_results=1)):
        with pytest.raises(TypeError):
            call()


# 4 ------------------------------------------------------------------------- unloaded blobs and the generation

def test_a_blob_is_unloaded_until_a_response_loads_it(gcs):
    put(gcs, V, b"abc", content_type="text/markdown")
    blob = gcs.client().bucket(V).blob(NAME)
    assert (blob.generation, blob.metageneration, blob.size, blob.content_type, blob.metadata) == (None,) * 5
    blob.reload()
    assert (blob.size, blob.content_type, blob.metageneration) == (3, "text/markdown", 1)


def test_a_generation_addressed_blob_reads_and_patches_that_version_and_erases_it_on_delete(gcs):
    a = put(gcs, V, b"v1")
    put(gcs, V, b"v2")
    old = gcs.client().bucket(V).blob(NAME, generation=a.generation)
    assert old.download_as_bytes() == b"v1" and old.exists()
    old.metadata = {"note": "kept"}
    old.patch()
    assert gcs.versions(V, NAME)[0].metadata == {"note": "kept"}
    assert gcs.versions(V, NAME)[1].metadata == {}
    gcs.client().bucket(V).blob(NAME, generation=a.generation).delete()
    assert [v.data for v in gcs.versions(V, NAME)] == [b"v2"]


def test_delete_after_patch_on_the_same_blob_erases_the_version(gcs):
    """The SDK trap of AGENT_WORKSPACE_RFC §5.3: patch() loads the generation, so delete() sends it and erases."""
    a = put(gcs, V, b"v1")
    blob = gcs.client().bucket(V).blob(NAME)
    blob.metadata = {"deleted_by": "chat"}
    blob.patch(if_generation_match=a.generation)
    assert blob.generation == a.generation
    blob.delete(if_generation_match=a.generation)
    assert gcs.versions(V, NAME) == []              # erased, not noncurrent


def test_delete_after_upload_and_after_reload_on_the_same_blob_erase(gcs):
    blob = put(gcs, V, b"v1")
    blob.delete()
    assert gcs.versions(V, NAME) == []
    put(gcs, V, b"v2")
    loaded = gcs.client().bucket(V).blob(NAME)
    loaded.reload()
    loaded.delete()
    assert gcs.versions(V, NAME) == []


def test_a_fresh_blob_deletes_the_live_object_only(gcs):
    a = put(gcs, V, b"v1")
    gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    assert [v.live for v in gcs.versions(V, NAME)] == [False]


# 5 ------------------------------------------------------------------------------------------------------ patch

def test_patch_merges_removes_and_bumps_metageneration_and_updated_only(gcs):
    blob = gcs.client().bucket(V).blob(NAME)
    blob.metadata = {"origin": "chat", "fire": "f"}
    blob.upload_from_string(b"v1")
    before = gcs.live(V, NAME)
    patcher = gcs.client().bucket(V).blob(NAME)
    patcher.metadata = {"fire": None, "deleted_by": "chat", "n": 7}
    patcher.patch(if_generation_match=before.generation)
    after = gcs.live(V, NAME)
    assert after.metadata == {"origin": "chat", "deleted_by": "chat", "n": "7"}
    assert (after.generation, after.metageneration) == (before.generation, 2)
    assert after.updated > before.updated and after.time_created == before.time_created
    assert (patcher.generation, patcher.metageneration) == (before.generation, 2)


def test_patch_sends_only_what_was_set(gcs):
    blob = gcs.client().bucket(V).blob(NAME)
    blob.metadata = {"origin": "chat"}
    blob.upload_from_string(b"v1", content_type="text/markdown")
    loaded = gcs.client().bucket(V).get_blob(NAME)
    loaded.content_type = "text/plain"
    loaded.patch()
    assert (gcs.live(V, NAME).metadata, gcs.live(V, NAME).content_type) == ({"origin": "chat"}, "text/plain")
    cleared = gcs.client().bucket(V).blob(NAME)
    cleared.metadata = None
    cleared.patch()
    assert gcs.live(V, NAME).metadata == {}


def test_patch_precondition(gcs):
    a = put(gcs, V, b"v1")
    p = gcs.client().bucket(V).blob(NAME)
    p.metadata = {"deleted_by": "chat"}
    with pytest.raises(PreconditionFailed):
        p.patch(if_generation_match=a.generation + 1)
    assert gcs.live(V, NAME).metadata == {} and gcs.live(V, NAME).metageneration == 1


def test_custom_time_cannot_move_earlier_or_be_removed(gcs):
    t = FAKE_EPOCH + timedelta(days=3)
    blob = gcs.client().bucket(V).blob(NAME)
    blob.custom_time = t
    blob.upload_from_string(b"v1")
    for value in (t - timedelta(seconds=1), None):
        p = gcs.client().bucket(V).blob(NAME)
        p.custom_time = value
        with pytest.raises(BadRequest):
            p.patch()
    later = gcs.client().bucket(V).blob(NAME)
    later.custom_time = t + timedelta(days=1)
    later.patch()
    assert gcs.live(V, NAME).custom_time == t + timedelta(days=1)


# 6 ---------------------------------------------------------------------------------------------- missing object

def test_missing_object_answers(gcs):
    bucket = gcs.client().bucket(V)
    for call in (bucket.blob(NAME).download_as_bytes, bucket.blob(NAME).delete, bucket.blob(NAME).patch,
                 bucket.blob(NAME).reload, lambda: bucket.blob(NAME).delete(if_generation_match=5),
                 lambda: bucket.blob(NAME, generation=5).delete()):
        with pytest.raises(NotFound):
            call()
    assert bucket.get_blob(NAME) is None and not bucket.blob(NAME).exists()
    a = put(gcs, V, b"v1")
    bucket.blob(NAME).delete(if_generation_match=a.generation)
    assert bucket.get_blob(NAME) is None and not bucket.blob(NAME).exists()     # only a noncurrent version
    assert bucket.get_blob(NAME, generation=a.generation).time_deleted is not None


# 7 ---------------------------------------------------------------------------------------------------- the lock

def test_concurrent_create_if_absent_has_exactly_one_winner(gcs):
    outcomes, start = [], threading.Barrier(8)

    def writer(i):
        start.wait()
        try:
            put(gcs, V, f"w{i}".encode(), if_generation_match=0)
            outcomes.append("ok")
        except PreconditionFailed:
            outcomes.append("412")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["412"] * 7 + ["ok"] and len(gcs.versions(V, NAME)) == 1


# 8 --------------------------------------------------------------------------------------------------- listing

def test_list_order_prefix_and_versions(gcs):
    for name in ("u1/b.md", "u1/a.md", "u1/projects/c.md", "u10/a.md"):
        put(gcs, V, b"x", name=name)
    put(gcs, V, b"y", name="u1/a.md")
    client = gcs.client()
    assert [b.name for b in client.list_blobs(V, prefix="u1/")] == ["u1/a.md", "u1/b.md", "u1/projects/c.md"]
    assert [b.name for b in client.bucket(V).list_blobs(prefix="u1/a")] == ["u1/a.md"]
    versions = list(client.list_blobs(client.bucket(V), prefix="u1/a.md", versions=True))
    assert [b.generation for b in versions] == sorted(b.generation for b in versions) and len(versions) == 2
    assert versions[0].time_deleted is not None and versions[0].size == 1      # loaded blobs
    assert len(list(client.list_blobs(V))) == 4


# 9 ------------------------------------------------------------------------------------------------ object names

@pytest.mark.parametrize("name", ["", "x" * 1025, "é" * 513, "a\nb", "a\rb", ".", ".."])
def test_invalid_object_names_are_refused(gcs, name):
    with pytest.raises(BadRequest):
        put(gcs, V, b"x", name=name)


def test_a_1024_byte_name_is_accepted(gcs):
    put(gcs, V, b"x", name="é" * 512)
    assert gcs.names(V) == ["é" * 512]


# 10 --------------------------------------------------------------------------------------------------- clock

def test_default_clock_advances_one_second_per_mutation_and_an_injected_clock_is_used():
    gcs = FakeGcs(buckets={V: "versioned"})
    a = put(gcs, V, b"v1")
    assert a.time_created == FAKE_EPOCH + timedelta(seconds=1)
    put(gcs, V, b"v2")
    assert gcs.live(V, NAME).time_created == FAKE_EPOCH + timedelta(seconds=2)
    fixed = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    pinned = FakeGcs(buckets={U: "plain"}, clock=lambda: fixed)
    assert put(pinned, U, b"v1").time_created == fixed


# faults and hooks ------------------------------------------------------------------------------------------

def test_fail_next_before_the_op_changes_nothing(gcs):
    gcs.fail_next("upload", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        put(gcs, V, b"v1")
    assert gcs.versions(V, NAME) == []
    put(gcs, V, b"v1")                              # consumed: the next one passes
    assert gcs.live(V, NAME).data == b"v1"


def test_fail_next_landed_applies_then_raises(gcs):
    a = put(gcs, V, b"v1")
    gcs.fail_next("delete", PreconditionFailed("412 on the retry"), landed=True)
    with pytest.raises(PreconditionFailed):
        gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    assert gcs.live(V, NAME) is None


def test_faults_are_consumed_in_order_per_op(gcs):
    put(gcs, V, b"v1")
    gcs.fail_next("download", ServiceUnavailable("first"))
    gcs.fail_next("download", NotFound("second"))
    blob = gcs.client().bucket(V).blob(NAME)
    with pytest.raises(ServiceUnavailable):
        blob.download_as_bytes()
    assert blob.exists()                            # another op is untouched
    with pytest.raises(NotFound):
        blob.download_as_bytes()
    assert blob.download_as_bytes() == b"v1"
    for op in ("get", "list", "exists", "patch"):
        gcs.fail_next(op, ServiceUnavailable(op))
    for call in (lambda: gcs.client().bucket(V).get_blob(NAME), lambda: gcs.client().list_blobs(V),
                 blob.exists, blob.patch):
        with pytest.raises(ServiceUnavailable):
            call()


def test_after_hook_runs_once_after_the_op_landed_outside_the_lock(gcs):
    a = put(gcs, V, b"v1")
    seen = []

    def overwrite():
        seen.append(gcs.live(V, NAME).metadata)                 # takes the lock: would deadlock under it
        put(gcs, V, b"v2", if_generation_match=a.generation)

    gcs.on("patch", overwrite)
    p = gcs.client().bucket(V).blob(NAME)
    p.metadata = {"deleted_by": "chat"}
    p.patch(if_generation_match=a.generation)
    assert seen == [{"deleted_by": "chat"}]                     # after the patch landed
    with pytest.raises(PreconditionFailed):
        gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    gcs.client().bucket(V).get_blob(NAME).patch()               # one shot: no second overwrite
    assert [v.data for v in gcs.versions(V, NAME)] == [b"v1", b"v2"]


def test_before_hook_runs_before_the_request_even_when_it_then_fails(gcs):
    a = put(gcs, V, b"v1")
    gcs.on("delete", lambda: put(gcs, V, b"v2", if_generation_match=a.generation), when="before")
    with pytest.raises(PreconditionFailed):         # the request meets the version the hook wrote
        gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    assert [v.data for v in gcs.versions(V, NAME)] == [b"v1", b"v2"]
    gcs.client().bucket(V).blob(NAME).delete()      # one shot
    assert gcs.live(V, NAME) is None and len(gcs.versions(V, NAME)) == 2


def test_an_after_hook_does_not_run_when_the_op_fails(gcs):
    ran = []
    gcs.on("delete", lambda: ran.append(True))
    with pytest.raises(NotFound):
        gcs.client().bucket(V).blob(NAME).delete()
    assert ran == []
    put(gcs, V, b"v1")
    gcs.client().bucket(V).blob(NAME).delete()
    assert ran == [True]


def test_unknown_op_or_when_is_refused(gcs):
    with pytest.raises(ValueError):
        gcs.fail_next("rewrite", ServiceUnavailable("x"))
    with pytest.raises(ValueError):
        gcs.on("compose", lambda: None)
    with pytest.raises(ValueError):
        gcs.on("delete", lambda: None, when="during")


# refusals, signed URLs, install -----------------------------------------------------------------------------

def test_access_control_is_refused(gcs):
    blob = put(gcs, U, b"x", content_type="application/pdf")
    with pytest.raises(AssertionError):
        blob.make_public()
    with pytest.raises(AssertionError):
        blob.acl
    with pytest.raises(AssertionError):
        put(gcs, U, b"y", predefined_acl="publicRead")
    assert gcs.live(U, NAME).data == b"x" and gcs.live(U, NAME).content_type == "application/pdf"


def test_upload_content_type_defaults_to_text_plain_and_str_data_is_utf8(gcs):
    put(gcs, U, "Rome hotel")
    assert (gcs.live(U, NAME).data, gcs.live(U, NAME).content_type) == (b"Rome hotel", "text/plain")


def test_signed_url_is_deterministic_recorded_and_needs_no_object(gcs):
    blob = gcs.client().bucket(U).blob("docs/u1/FA-0137.pdf")
    kw = dict(version="v4", expiration=timedelta(seconds=300), method="GET",
              service_account_email="sa@example.com", access_token="tok")
    url = blob.generate_signed_url(**kw)
    assert url == blob.generate_signed_url(**kw)
    assert "media-test/docs/u1/FA-0137.pdf" in url and "X-Goog-Expires=300" in url
    assert gcs.signed_urls[0] == {"bucket": U, "name": "docs/u1/FA-0137.pdf", **kw}


def test_signed_url_over_seven_days_is_refused(gcs):
    blob = gcs.client().bucket(U).blob("docs/u1/FA-0137.pdf")
    blob.generate_signed_url(version="v4", expiration=timedelta(days=7), method="GET")
    with pytest.raises(ValueError):
        blob.generate_signed_url(version="v4", expiration=timedelta(days=7, seconds=1), method="GET")
    with pytest.raises(NotImplementedError):
        blob.generate_signed_url(version="v4", expiration=300, method="GET")
    assert len(gcs.signed_urls) == 1


def test_install_patches_the_storage_client(gcs, monkeypatch):
    from google.cloud import storage
    client = gcs.install(monkeypatch)
    assert storage.Client() is client and storage.Client(project="p") is client
    storage.Client().bucket(U).blob("k").upload_from_string(b"x")
    assert gcs.names(U) == ["k"]


def test_reload_and_get_blob_load_every_property(gcs):
    blob = gcs.client().bucket(V).blob(NAME)
    blob.metadata = {"origin": "chat"}
    blob.custom_time = FAKE_EPOCH
    blob.upload_from_string(b"abc", content_type="text/markdown")
    fresh = gcs.client().bucket(V).blob(NAME)
    fresh.reload()
    got = gcs.client().bucket(V).get_blob(NAME)
    for b in (fresh, got):
        assert (b.generation, b.metageneration, b.size, b.content_type, b.metadata, b.custom_time, b.time_deleted) == (
            blob.generation, 1, 3, "text/markdown", {"origin": "chat"}, FAKE_EPOCH, None)
        assert b.time_created == b.updated
