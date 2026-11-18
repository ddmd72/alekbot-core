"""The GCS kits' backend seam, offline (tests/support/gcs_kit_backend.py). The "real" storage client here is the shared fake's
client over one versioned bucket, standing in for the test bucket: what the seam sends to it (prefixed names, the
requests around faults and hooks) is read back from it. The run on the test bucket itself is
`tests/cloud/test_kits_cloud.py`."""
import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed, ServiceUnavailable

from tests.support.gcs_fake import FakeGcs
from tests.support.gcs_kit_backend import RealKitGcsBackend, run_prefix

TEST_BUCKET = "kits-test"
ROOT = "kits/r1/0/"


@pytest.fixture
def under():
    """The stand-in for the versioned test bucket."""
    return FakeGcs(buckets={TEST_BUCKET: "versioned"})


@pytest.fixture
def backend(under):
    return RealKitGcsBackend(under.client(), TEST_BUCKET, ROOT)


def test_the_unit_suite_kit_gcs_is_the_fake(kit_gcs):
    assert kit_gcs is FakeGcs


def test_each_test_gets_a_fresh_prefix_of_the_run():
    prefixes = run_prefix("r1")
    assert [next(prefixes), next(prefixes)] == ["kits/r1/0/", "kits/r1/1/"]


def test_names_are_prefixed_on_the_bucket_and_stripped_for_the_adapter(under, backend):
    gcs = backend(buckets={"media-test": "plain"})
    blob = gcs.client().bucket("media-test").blob("u1/files/FA-0137.pdf")
    blob.upload_from_string(b"%PDF", content_type="application/pdf")
    assert under.names(TEST_BUCKET) == [ROOT + "media-test/u1/files/FA-0137.pdf"]
    assert blob.name == "u1/files/FA-0137.pdf" and blob.generation is not None
    assert [b.name for b in gcs.client().list_blobs("media-test", prefix="u1/")] == ["u1/files/FA-0137.pdf"]
    got = gcs.client().bucket("media-test").get_blob("u1/files/FA-0137.pdf")
    assert (got.name, got.content_type, got.download_as_bytes()) == ("u1/files/FA-0137.pdf", "application/pdf", b"%PDF")
    assert gcs.client().bucket("media-test").get_blob("u1/files/none.pdf") is None


def test_writable_properties_reach_the_real_blob_and_read_only_ones_raise(under, backend):
    gcs = backend(buckets={"workspace-test": "versioned"})
    blob = gcs.client().bucket("workspace-test").blob("u1/projects/a.md")
    blob.metadata = {"origin": "chat"}
    blob.upload_from_string(b"Lisbon trip notes", content_type="text/markdown")
    assert under.live(TEST_BUCKET, ROOT + "workspace-test/u1/projects/a.md").metadata == {"origin": "chat"}
    with pytest.raises(AttributeError):
        blob.generation = 5


def test_installed_client_is_what_storage_client_returns(backend, monkeypatch):
    from google.cloud import storage
    gcs = backend(buckets={"media-test": "plain"})
    gcs.install(monkeypatch)
    storage.Client().bucket("media-test").blob("docs/u1/a.txt").upload_from_string(b"x")
    assert gcs.names("media-test") == ["docs/u1/a.txt"]


def test_two_declared_buckets_never_see_each_other(backend):
    gcs = backend(buckets={"media-test": "plain", "workspace-test": "versioned"})
    gcs.client().bucket("media-test").blob("a.md").upload_from_string(b"media")
    assert gcs.names("workspace-test") == [] and not gcs.client().bucket("workspace-test").blob("a.md").exists()
    assert gcs.names("media-test") == ["a.md"]


def test_an_undeclared_bucket_is_not_found(backend):
    gcs = backend(buckets={"media-test": "plain"})
    with pytest.raises(NotFound):
        gcs.client().bucket("other-test").blob("a.md").upload_from_string(b"x")


def test_a_plain_bucket_is_read_as_its_live_versions(under, backend):
    """The test bucket is versioned: an overwrite and a delete leave noncurrent versions there, which a plain bucket would
    not keep; the inspection of a "plain" bucket shows what a plain bucket keeps."""
    gcs = backend(buckets={"media-test": "plain", "workspace-test": "versioned"})
    for bucket in ("media-test", "workspace-test"):
        blob = gcs.client().bucket(bucket)
        blob.blob("a.md").upload_from_string(b"first")
        blob.blob("a.md").upload_from_string(b"second")
        blob.blob("b.md").upload_from_string(b"gone")
        blob.blob("b.md").delete()
    assert [v.data for v in gcs.versions("media-test", "a.md")] == [b"second"]
    assert gcs.versions("media-test", "b.md") == []
    assert [(v.data, v.live) for v in gcs.versions("workspace-test", "a.md")] == [(b"first", False), (b"second", True)]
    assert [v.live for v in gcs.versions("workspace-test", "b.md")] == [False]
    assert gcs.live("media-test", "a.md").data == b"second" and gcs.live("media-test", "b.md") is None
    assert gcs.names("media-test") == ["a.md"]
    assert len(under.versions(TEST_BUCKET, ROOT + "media-test/a.md")) == 2       # the bucket itself keeps both


def test_a_fault_raises_before_the_request_or_after_it_landed(backend):
    gcs = backend(buckets={"media-test": "plain"})
    blob = gcs.client().bucket("media-test").blob("a.md")
    gcs.fail_next("upload", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        blob.upload_from_string(b"x")
    assert gcs.names("media-test") == []
    gcs.fail_next("upload", PreconditionFailed("412"), landed=True)
    with pytest.raises(PreconditionFailed):
        gcs.client().bucket("media-test").blob("a.md").upload_from_string(b"y")
    assert gcs.live("media-test", "a.md").data == b"y"
    backend.teardown()


def test_hooks_run_before_or_after_the_request(backend):
    gcs = backend(buckets={"workspace-test": "versioned"})
    seen = []
    gcs.on("delete", lambda: seen.append(("before", gcs.names("workspace-test"))), when="before")
    gcs.on("delete", lambda: seen.append(("after", gcs.names("workspace-test"))))
    gcs.client().bucket("workspace-test").blob("a.md").upload_from_string(b"x")
    gcs.client().bucket("workspace-test").blob("a.md").delete()
    assert seen == [("before", ["a.md"]), ("after", [])]


def test_reads_lists_and_exists_are_ops_too(backend):
    gcs = backend(buckets={"workspace-test": "versioned"})
    bucket = gcs.client().bucket("workspace-test")
    bucket.blob("a.md").upload_from_string(b"x")
    for op, call in (("get", lambda: bucket.get_blob("a.md")), ("get", lambda: bucket.blob("a.md").reload()),
                     ("download", lambda: bucket.blob("a.md").download_as_bytes()),
                     ("exists", lambda: bucket.blob("a.md").exists()),
                     ("list", lambda: list(gcs.client().list_blobs("workspace-test"))),
                     ("patch", lambda: bucket.get_blob("a.md").patch())):
        gcs.fail_next(op, ServiceUnavailable(op))
        with pytest.raises(ServiceUnavailable):
            call()
    backend.teardown()


def test_inspection_consumes_no_fault(backend):
    gcs = backend(buckets={"workspace-test": "versioned"})
    gcs.client().bucket("workspace-test").blob("a.md").upload_from_string(b"x")
    gcs.fail_next("list", ServiceUnavailable("503"))
    gcs.fail_next("download", ServiceUnavailable("503"))
    assert gcs.names("workspace-test") == ["a.md"] and gcs.live("workspace-test", "a.md").data == b"x"
    with pytest.raises(pytest.fail.Exception, match="never reached on the real backend"):
        backend.teardown()


def test_a_planned_fault_or_hook_never_reached_fails_the_test(backend):
    gcs = backend(buckets={"media-test": "plain"})
    gcs.on("get", lambda: None)
    with pytest.raises(pytest.fail.Exception, match=r"on\('get', when='after'\)"):
        backend.teardown()


def test_the_prefix_is_erased_before_first_use_and_at_teardown(under, backend):
    under.client().bucket(TEST_BUCKET).blob(ROOT + "media-test/left-over.md").upload_from_string(b"crashed run")
    gcs = backend(buckets={"media-test": "plain"})
    assert gcs.names("media-test") == [] and under.versions(TEST_BUCKET, ROOT + "media-test/left-over.md") == []
    gcs.client().bucket("media-test").blob("a.md").upload_from_string(b"x")
    under.client().bucket(TEST_BUCKET).blob("parity/elsewhere.md").upload_from_string(b"not ours")
    backend.teardown()
    assert under.names(TEST_BUCKET) == ["parity/elsewhere.md"]


def test_access_changes_are_refused(backend):
    gcs = backend(buckets={"media-test": "plain"})
    blob = gcs.client().bucket("media-test").blob("a.md")
    with pytest.raises(AssertionError):
        blob.upload_from_string(b"x", predefined_acl="publicRead")
    with pytest.raises(AssertionError):
        blob.make_public()
    with pytest.raises(AssertionError):
        blob.acl
    assert gcs.names("media-test") == []


def test_fake_only_settings_raise(backend):
    with pytest.raises(ValueError, match="gcs_fake_only"):
        backend(buckets={"media-test": "plain"}, clock=lambda: None)
    gcs = backend(buckets={"media-test": "plain"})
    with pytest.raises(AttributeError, match="gcs_fake_only"):
        gcs.signed_urls
    with pytest.raises(ValueError, match="one kit_gcs per test"):
        backend(buckets={"media-test": "plain"})
