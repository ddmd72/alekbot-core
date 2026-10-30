"""The owner's live check (USER_DRIVE_INDEXING_RFC §22, §24.12), offline on the in-memory repository: a local folder
becomes a drive, the real tick indexes it under a synthetic user, queries come back as Smart reads them, and the
production purge leaves nothing; it never writes under a real user id."""
from datetime import datetime

import pytest

from scripts.validation.drive_eval.eval_connection import EVAL_CONNECTION_ID
from scripts.validation.drive_eval.live_check import LiveCheckError, live_check, manifest_from_folder
from scripts.validation.drive_eval.mini_pdf import text_pdf
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import fake_content, rome

USER = "zz-synthetic-test"


@pytest.fixture
def folder(tmp_path):
    from PIL import Image

    (tmp_path / "Scans").mkdir()
    (tmp_path / "Scans" / "electricity bill.pdf").write_bytes(text_pdf(["Electricity bill, March 2026"]))
    (tmp_path / "Photos").mkdir()
    img = Image.new("RGB", (40, 30), "blue")
    exif = img.getexif()
    exif[271] = "Apple"
    exif.get_ifd(0x8769)[36867] = "2026:03:14 20:00:00"
    img.save(tmp_path / "Photos" / "porto.jpg", exif=exif)
    (tmp_path / ".DS_Store").write_bytes(b"x")
    return tmp_path


def test_a_folder_becomes_a_manifest(folder):
    items = {i["path"]: i for i in manifest_from_folder(folder)["items"]}
    assert set(items) == {"Scans", "Scans/electricity bill.pdf", "Photos", "Photos/porto.jpg"}
    assert items["Scans"]["folder"] and items["Scans/electricity bill.pdf"]["mime"] == "application/pdf"
    photo = items["Photos/porto.jpg"]
    assert photo["content"] == "Photos/porto.jpg" and photo["facets"]["image"] == {"width": 40, "height": 30}
    assert photo["facets"]["photo"] == {"cameraMake": "Apple", "takenDateTime": "2026-03-14T20:00:00Z"}
    datetime.fromisoformat(items["Scans"]["fs_modified"].replace("Z", "+00:00"))


async def test_index_query_then_purge_leaves_nothing(folder):
    repo = InMemoryDriveIndexRepository()
    report = await live_check(folder, USER, [{"query": "electricity bill", "filters": {}}], repo=repo,
                              embeddings=FakeEmbeddings(), embedding_space="fake/8", place_of=rome,
                              throttle_pause_s=0, **fake_content())
    assert report.indexed.get("carded", 0) >= 2 and report.purged
    (answer,) = report.answers
    assert answer["status"] == "ok" and "Scans/electricity bill.pdf" in answer["paths"]
    assert "<drive_document" in answer["text"]
    assert await repo.get_sync_state(ConnectionScope(USER, EVAL_CONNECTION_ID)) is None
    assert not [k for k in repo.records if k[0] == EVAL_CONNECTION_ID] and not [k for k in repo.chunks if k[0] == EVAL_CONNECTION_ID]


async def test_a_real_user_id_is_refused(folder):
    with pytest.raises(LiveCheckError, match="zz-synthetic-"):
        await live_check(folder, "owner-123", [], repo=InMemoryDriveIndexRepository(), embeddings=FakeEmbeddings(),
                         embedding_space="fake/8", place_of=rome)


async def test_an_existing_synthetic_index_is_refused(folder):
    repo = InMemoryDriveIndexRepository()
    await live_check(folder, USER, [], repo=repo, embeddings=FakeEmbeddings(), embedding_space="fake/8",
                     place_of=rome, throttle_pause_s=0, **fake_content())
    from src.domain.drive_index import DriveSyncState
    await repo.save_sync_state(DriveSyncState(user_id=USER, connection_id=EVAL_CONNECTION_ID))
    with pytest.raises(LiveCheckError, match="already has an index"):
        await live_check(folder, USER, [], repo=repo, embeddings=FakeEmbeddings(), embedding_space="fake/8",
                         place_of=rome)


async def test_a_failing_query_still_purges(folder, monkeypatch):
    from scripts.validation.drive_eval import live_check as module

    def broken(case):
        raise ValueError("bad query")

    monkeypatch.setattr(module, "request_for", broken)
    repo = InMemoryDriveIndexRepository()
    with pytest.raises(ValueError, match="bad query"):
        await live_check(folder, USER, [{"query": "x", "filters": {}}], repo=repo, embeddings=FakeEmbeddings(),
                         embedding_space="fake/8", place_of=rome, throttle_pause_s=0, **fake_content())
    assert await repo.get_sync_state(ConnectionScope(USER, EVAL_CONNECTION_ID)) is None and not [k for k in repo.records if k[0] == EVAL_CONNECTION_ID]
