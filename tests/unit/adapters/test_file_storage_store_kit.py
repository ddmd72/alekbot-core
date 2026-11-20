"""P11 card B: the real GcsFileStorageAdapter behind FileStoragePort, over the shared GCS fake (tests/support/gcs_fake.py,
a plain bucket like the media bucket). Writes, Finder-style names, tenancy, reads, errors, and two defects:
P11-G4-2 (two concurrent uploads of one name) and P11-G4-3 (delete of a missing upload)."""
import asyncio
import unicodedata

import pytest
from google.api_core.exceptions import PreconditionFailed, ServiceUnavailable

from src.adapters import gcs_file_storage_adapter
from src.adapters.gcs_file_storage_adapter import GcsFileStorageAdapter
from tests.support.gcs_fake import FakeGcs
from tests.support.redelivery_world import Rendezvous, require

BUCKET = "media-test"


def _adapter(fake: FakeGcs) -> GcsFileStorageAdapter:
    adapter = GcsFileStorageAdapter(BUCKET)
    adapter._client = fake.client()      # the adapter's injection point (its lazy client); no constructor argument
    return adapter


@pytest.fixture
def fake(kit_gcs):
    return kit_gcs(buckets={BUCKET: "plain"})


@pytest.fixture
def store(fake):
    return _adapter(fake)


async def test_upload_stores_bytes_and_content_type_under_the_user(fake, store):
    name = await store.upload(b"%PDF invoice FA-0137", "FA-0137.pdf", "u1", "application/pdf")
    assert name == "FA-0137.pdf"
    obj = fake.live(BUCKET, "u1/files/FA-0137.pdf")
    assert (obj.data, obj.content_type) == (b"%PDF invoice FA-0137", "application/pdf")
    assert fake.names(BUCKET) == ["u1/files/FA-0137.pdf"]


async def test_upload_sanitises_and_normalises(fake, store):
    weird = await store.upload(b"x", "a#b?c[d]*e:f.txt", "u1", "text/plain")
    nfd = unicodedata.normalize("NFD", "Lúcia-lease.pdf")
    nfc = unicodedata.normalize("NFC", "Lúcia-lease.pdf")
    lease = await store.upload(b"lease", nfd, "u1", "application/pdf")
    assert weird == "a_b_c_d__e_f.txt" and lease == nfc and nfd != nfc
    assert fake.names(BUCKET) == sorted([f"u1/files/{nfc}", "u1/files/a_b_c_d__e_f.txt"])


async def test_serial_same_name_uploads_are_finder_numbered(store):
    images = [await store.upload(f"image {i}".encode(), "image.png", "u1", "image/png") for i in range(3)]
    notes = [await store.upload(f"notes {i}".encode(), "notes", "u1", "text/plain") for i in range(2)]
    assert images == ["image.png", "image (1).png", "image (2).png"]
    assert notes == ["notes", "notes (1)"]
    assert [await store.download(n, "u1") for n in images] == [b"image 0", b"image 1", b"image 2"]
    assert [await store.download(n, "u1") for n in notes] == [b"notes 0", b"notes 1"]


async def test_dedup_is_per_user(store):
    await store.upload(b"u2's", "image.png", "u2", "image/png")
    assert await store.upload(b"u1's", "image.png", "u1", "image/png") == "image.png"
    assert await store.download("image.png", "u2") == b"u2's"


async def test_download_round_trip_and_nfd_lookup(store):
    name = await store.upload(b"lease", "Lúcia-lease.pdf", "u1", "application/pdf")
    assert await store.download(name, "u1") == b"lease"
    assert await store.download(unicodedata.normalize("NFD", name), "u1") == b"lease"


async def test_download_missing_and_foreign_is_file_not_found(store):
    await store.upload(b"u2's", "image.png", "u2", "image/png")
    with pytest.raises(FileNotFoundError):
        await store.download("image.png", "u1")


async def test_download_outage_is_not_file_not_found(fake, store):
    await store.upload(b"x", "Tom's plan.docx", "u1", "application/octet-stream")
    fake.fail_next("download", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await store.download("Tom's plan.docx", "u1")


async def test_delete_removes_the_object(fake, store):
    await store.upload(b"x", "birth-certificate.pdf", "u1", "application/pdf")
    await store.delete("birth-certificate.pdf", "u1")
    assert fake.versions(BUCKET, "u1/files/birth-certificate.pdf") == []
    assert not await store.exists("birth-certificate.pdf", "u1")
    with pytest.raises(FileNotFoundError):
        await store.download("birth-certificate.pdf", "u1")


async def test_exists_is_per_user(store):
    await store.upload(b"x", "image.png", "u2", "image/png")
    assert not await store.exists("image.png", "u1")
    assert await store.exists("image.png", "u2")


async def test_upload_failure_returns_nothing(fake, store):
    fake.fail_next("upload", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await store.upload(b"x", "FA-0137.pdf", "u1", "application/pdf")
    assert fake.names(BUCKET) == []


async def test_delete_of_a_missing_file_is_file_not_found(store):
    """`open_file`'s delete answers "not found — nothing to delete" only on FileNotFoundError; the SDK's NotFound
    becomes "Could not delete" and an ERROR. Flips when `delete` maps NotFound to FileNotFoundError, as `download`
    does. Control: test_delete_removes_the_object."""
    err = None
    try:
        await store.delete("FA-0137.pdf", "u1")
    except Exception as exc:
        err = exc
    require(err is not None, "delete of a missing file raised")
    assert isinstance(err, FileNotFoundError)


async def test_concurrent_same_name_uploads_get_two_names(store):
    """Slack names every pasted image `image.png`; two at once (two channels, Slack and Telegram) both see the name
    free, both return it, and the second write replaces the first image. Flips when the upload is conditional
    (`if_generation_match=0`) and a 412 moves on to the next Finder candidate: both uploads still reach the rendezvous
    after `exists`. A per-process lock is not a fix (the uploads can run on two instances): under it the loser waits
    while the winner holds the rendezvous, `rv.stalled` is set and the test reports FAILED, which reads "wrong fix",
    not "broken test". Control: test_serial_same_name_uploads_are_finder_numbered."""
    rv = Rendezvous()
    rv.wrap(store, "exists")

    async def upload(data: bytes) -> str:
        try:
            return await store.upload(data, "image.png", "u1", "image/png")
        finally:
            rv.done()

    names = await asyncio.gather(upload(b"first"), upload(b"second"))
    require(rv.met and not rv.stalled, "both uploads checked the name before either wrote")
    assert len(set(names)) == 2 and {await store.download(n, "u1") for n in names} == {b"first", b"second"}


async def test_retried_upload_that_had_landed_keeps_its_name(fake, store):
    """P11-G4-2, the read-back: the client retries a conditional write whose response was lost and meets its own
    object (412). The upload is the one stored, under the first name; no second copy is written."""
    fake.fail_next("upload", PreconditionFailed("412"), landed=True)
    name = await store.upload(b"%PDF invoice FA-0137", "FA-0137.pdf", "u1", "application/pdf")
    assert name == "FA-0137.pdf"
    assert fake.names(BUCKET) == ["u1/files/FA-0137.pdf"]
    assert await store.download(name, "u1") == b"%PDF invoice FA-0137"


async def test_upload_that_never_gets_a_name_fails_and_writes_nothing(fake, store, monkeypatch):
    """P11-G4-2: every chosen name taken at write time, attempt after attempt: the upload fails loudly, it never
    replaces a file."""
    monkeypatch.setattr(gcs_file_storage_adapter, "_MAX_NAME_ATTEMPTS", 2)
    fake.fail_next("upload", PreconditionFailed("412"))
    fake.fail_next("upload", PreconditionFailed("412"))
    with pytest.raises(PreconditionFailed):
        await store.upload(b"x", "image.png", "u1", "image/png")
    assert fake.names(BUCKET) == []


async def test_delete_outage_is_not_file_not_found(fake, store):
    """Control for P11-G4-3: only a missing object is FileNotFoundError; an outage stays an outage."""
    await store.upload(b"x", "FA-0137.pdf", "u1", "application/pdf")
    fake.fail_next("delete", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await store.delete("FA-0137.pdf", "u1")
    assert fake.names(BUCKET) == ["u1/files/FA-0137.pdf"]


@pytest.mark.parametrize("given, stored", [
    ("file\nname.txt", "file_name.txt"),
    ("file\tname.txt", "file_name.txt"),
    ("file\rname.txt", "file_name.txt"),
    ("звіт.docx", "звіт.docx"),
    ("report-2026.docx", "report-2026.docx"),
])
async def test_upload_replaces_control_characters_and_keeps_clean_names(fake, store, given, stored):
    assert await store.upload(b"x", given, "u1", "text/plain") == stored
    assert fake.names(BUCKET) == [f"u1/files/{stored}"]


async def test_upload_stamps_a_fresh_upload_id(fake, store):
    first = await store.upload(b"x", "image.png", "u1", "image/png")
    second = await store.upload(b"y", "image.png", "u1", "image/png")
    ids = [fake.live(BUCKET, f"u1/files/{name}").metadata for name in (first, second)]
    assert all(set(m) == {"upload_id"} and len(m["upload_id"]) == 32 for m in ids)
    assert ids[0]["upload_id"] != ids[1]["upload_id"]


async def test_download_missing_names_the_file(store):
    with pytest.raises(FileNotFoundError) as err:
        await store.download("gone.csv", "u1")
    assert str(err.value) == "gone.csv"


async def test_get_url_is_the_bucket_url_of_the_users_file(store):
    url = await store.get_url("report (1).docx", "u1")
    assert url == f"https://storage.googleapis.com/{BUCKET}/u1/files/report (1).docx"
