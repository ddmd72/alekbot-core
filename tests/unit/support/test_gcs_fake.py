"""Self-tests of the shared GCS fake (tests/support/gcs_fake.py): one test per semantic of its module docstring, plus
faults, hooks, the private-bucket refusal, signed URLs and the install helper. What real GCS settles is in the parity
scenarios (tests/support/gcs_parity.py)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import BadRequest, NotFound, PreconditionFailed, ServiceUnavailable

from tests.support.gcs_fake import FAKE_EPOCH, FakeGcs

V, U = "ws-test", "media-test"       # a versioned and an unversioned bucket
NAME = "u1/projects/a.md"


@pytest.fixture
def gcs():
    return FakeGcs(versioned={V})


def put(gcs, bucket, data, name=NAME, **kw):
    blob = gcs.client().bucket(bucket).blob(name)
    blob.upload_from_string(data, **kw)
    return blob


# 1 --------------------------------------------------------------------------------------------- generations

def test_generations_increase_per_bucket_and_metageneration_starts_at_one(gcs):
    a, b = put(gcs, V, b"v1"), put(gcs, V, b"v2")
    other = put(gcs, U, b"x")
    assert b.generation > a.generation
    assert other.generation == a.generation         # per bucket: each bucket counts on its own
    assert (a.metageneration, b.metageneration) == (1, 1)


# 2 ----------------------------------------------------------------------------------------------- versioning

def test_versioned_overwrite_keeps_a_noncurrent_version(gcs):
    a = put(gcs, V, b"v1")
    b = put(gcs, V, b"v2")
    old, new = gcs.versions(V, NAME)
    assert (old.generation, old.data, old.live, new.generation, new.live) == (a.generation, b"v1", False, b.generation, True)
    assert old.time_deleted == new.time_created


def test_unversioned_overwrite_and_delete_erase(gcs):
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
    gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    put(gcs, V, b"v3", if_generation_match=0)       # only noncurrent versions: they do not count
    assert gcs.live(V, NAME).data == b"v3"


def test_every_precondition_kind(gcs):
    a = put(gcs, V, b"v1")
    blob = gcs.client().bucket(V).blob(NAME)
    for kw in ({"if_generation_match": a.generation + 1}, {"if_generation_not_match": a.generation},
               {"if_metageneration_match": 2}, {"if_metageneration_not_match": 1}):
        with pytest.raises(PreconditionFailed):
            blob.download_as_bytes(**kw)
    assert blob.download_as_bytes(if_generation_match=a.generation, if_metageneration_match=1) == b"v1"
    with pytest.raises(PreconditionFailed):
        put(gcs, V, b"x", name="u1/new.md", if_metageneration_match=1)     # missing object: no metageneration


# 4 ------------------------------------------------------------------------------------- generation-addressed

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


def test_an_uploaded_blob_is_generation_addressed_so_its_delete_erases(gcs):
    blob = put(gcs, V, b"v1")
    blob.delete()                                   # the RFC §5.3 trap: the SDK sends blob.generation
    assert gcs.versions(V, NAME) == []


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
                 bucket.blob(NAME).reload, lambda: bucket.blob(NAME, generation=5).delete()):
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

def test_list_order_prefix_delimiter_versions_and_max_results(gcs):
    for name in ("u1/b.md", "u1/a.md", "u1/projects/c.md", "u10/a.md"):
        put(gcs, V, b"x", name=name)
    put(gcs, V, b"y", name="u1/a.md")
    client = gcs.client()
    assert [b.name for b in client.list_blobs(V, prefix="u1/")] == ["u1/a.md", "u1/b.md", "u1/projects/c.md"]
    versions = list(client.list_blobs(V, prefix="u1/a.md", versions=True))
    assert [b.generation for b in versions] == sorted(b.generation for b in versions) and len(versions) == 2
    assert versions[0].time_deleted is not None and versions[0].size == 1      # loaded blobs
    listing = client.list_blobs(client.bucket(V), prefix="u1/", delimiter="/")
    assert [b.name for b in listing] == ["u1/a.md", "u1/b.md"] and listing.prefixes == {"u1/projects/"}
    assert [b.name for b in client.bucket(V).list_blobs(prefix="u1/", max_results=1)] == ["u1/a.md"]


def test_list_keywords_the_fake_does_not_model_fail_loudly(gcs):
    with pytest.raises(NotImplementedError):
        gcs.client().list_blobs(V, match_glob="**.md")
    with pytest.raises(NotImplementedError):
        gcs.client().list_blobs(V, page_token="t")


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
    gcs = FakeGcs(versioned={V})
    a = put(gcs, V, b"v1")
    assert a.time_created == FAKE_EPOCH + timedelta(seconds=1)
    put(gcs, V, b"v2")
    assert gcs.live(V, NAME).time_created == FAKE_EPOCH + timedelta(seconds=2)
    fixed = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    pinned = FakeGcs(clock=lambda: fixed)
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


def test_hook_runs_once_after_the_op_landed_and_may_call_the_fake(gcs):
    a = put(gcs, V, b"v1")
    seen = []

    def overwrite():
        seen.append(gcs.live(V, NAME).metadata)
        put(gcs, V, b"v2", if_generation_match=a.generation)

    gcs.on("patch", overwrite)
    p = gcs.client().bucket(V).blob(NAME)
    p.metadata = {"deleted_by": "chat"}
    p.patch(if_generation_match=a.generation)
    assert seen == [{"deleted_by": "chat"}]         # after the patch landed
    with pytest.raises(PreconditionFailed):
        gcs.client().bucket(V).blob(NAME).delete(if_generation_match=a.generation)
    p.patch()                                       # one shot: no second overwrite
    assert [v.data for v in gcs.versions(V, NAME)] == [b"v1", b"v2"]


def test_a_hook_does_not_run_when_the_op_fails(gcs):
    ran = []
    gcs.on("delete", lambda: ran.append(True))
    with pytest.raises(NotFound):
        gcs.client().bucket(V).blob(NAME).delete()
    assert ran == []
    put(gcs, V, b"v1")
    gcs.client().bucket(V).blob(NAME).delete()
    assert ran == [True]


def test_unknown_op_is_refused(gcs):
    with pytest.raises(ValueError):
        gcs.fail_next("rewrite", ServiceUnavailable("x"))
    with pytest.raises(ValueError):
        gcs.on("compose", lambda: None)


# private bucket, signed URLs, install -----------------------------------------------------------------------

def test_access_control_is_refused(gcs):
    blob = put(gcs, U, b"x", content_type="application/pdf")
    with pytest.raises(AssertionError):
        blob.make_public()
    with pytest.raises(AssertionError):
        blob.acl
    with pytest.raises(AssertionError):
        put(gcs, U, b"y", predefined_acl="publicRead")
    assert gcs.live(U, NAME).content_type == "application/pdf"


def test_upload_content_type_defaults_and_str_data(gcs):
    put(gcs, U, "text")
    assert (gcs.live(U, NAME).data, gcs.live(U, NAME).content_type) == (b"text", "text/plain")


def test_signed_url_is_deterministic_recorded_and_needs_no_object(gcs):
    blob = gcs.client().bucket(U).blob("docs/u1/FA-0137.pdf")
    url = blob.generate_signed_url(version="v4", expiration=timedelta(seconds=300), method="GET",
                                   service_account_email="sa@example.com", access_token="tok")
    assert url == blob.generate_signed_url(version="v4", expiration=timedelta(seconds=300), method="GET",
                                           service_account_email="sa@example.com", access_token="tok")
    assert "media-test/docs/u1/FA-0137.pdf" in url and "X-Goog-Expires=300" in url
    assert gcs.signed_urls[0] == {"bucket": U, "name": "docs/u1/FA-0137.pdf", "expiration": timedelta(seconds=300),
                                  "method": "GET", "version": "v4", "service_account_email": "sa@example.com",
                                  "access_token": "tok"}


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
