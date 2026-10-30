"""Exception text stays out of the T2 log lines (USER_DRIVE_INDEXING_RFC §27.12, Δ233; final review M3): the helper
`exc_brief` gives the type and a 12-hex digest of the message, and the read side's mirror lookups log that instead of
a traceback. Synthetic values only."""
import hashlib
import logging
from unittest.mock import AsyncMock, MagicMock

from src.domain.drive_mirror import KIND_FILE, MIRROR_ARCHIVED, MirrorDoc
from src.ports.drive_mirror_repository import DriveMirrorRepository
from src.services.drive_search_service import DriveSearchService
from src.utils.logger import exc_brief
from tests.support.drive_connection_service_fake import connection

SECRET = "drive item not found: /Apps/Alek/Secret Interview.mp4"


def test_exc_brief_is_type_and_digest_only():
    exc = FileNotFoundError(SECRET)
    text = exc_brief(exc)
    assert text == f"FileNotFoundError (message digest {hashlib.sha256(SECRET.encode()).hexdigest()[:12]})"
    assert "Secret" not in text


def _service(mirror) -> DriveSearchService:
    return DriveSearchService(MagicMock(), MagicMock(), MagicMock(), AsyncMock(), embedding_space="s", mirror=mirror)


async def test_mirror_path_lookup_failure_logs_no_message(caplog):
    mirror = AsyncMock(spec=DriveMirrorRepository)
    mirror.get_docs.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.DEBUG):
        assert await _service(mirror)._active_mirror_path(connection("u"), "fake_a:drive-1:item") == ""
    lines = [r for r in caplog.records if "mirror path lookup failed" in r.getMessage()]
    assert lines and all(r.exc_info is None for r in lines)
    assert not any("Secret" in r.getMessage() for r in caplog.records)


async def test_deleted_search_store_read_failure_logs_no_message(caplog):
    repo = AsyncMock()
    repo.get_transcript.side_effect = RuntimeError(SECRET)
    svc = DriveSearchService(repo, MagicMock(), MagicMock(), AsyncMock(), embedding_space="s",
                             mirror=AsyncMock(spec=DriveMirrorRepository))
    doc = MirrorDoc(user_id="u", drive_identity="d", kind=KIND_FILE, source_item_id="s", state=MIRROR_ARCHIVED,
                    content_key="k")
    with caplog.at_level(logging.DEBUG):
        assert await svc._stored("u", doc) is None
    lines = [r for r in caplog.records if "store read failed" in r.getMessage()]
    assert lines and all(r.exc_info is None for r in lines)
    assert not any("Secret" in r.getMessage() for r in caplog.records)
