"""
Unit tests for FileConversionService's `skill:` ref handling (RFC §15.4).

`skill:<name>/<path>` refs route straight to the injected SkillFileResolver — no mime
guessing, no FileStoragePort/MediaStoragePort involvement. Bare uploads and delivered-
document keys (docs/, email_review/, deep_research/, video_generation/) are unaffected.
"""

import pytest
from unittest.mock import AsyncMock

from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService


@pytest.fixture
def mock_storage():
    return AsyncMock(spec=FileStoragePort)


@pytest.fixture
def resolver():
    r = AsyncMock()
    r.read = AsyncMock(return_value="placeholder")
    return r


class TestResolveContentSkillRef:

    @pytest.mark.parametrize(
        "ref, content",
        [
            ("skill:fs/data.json", '{"a": 1}'),
            ("skill:fs/notes.md", "# heading\nbody"),
            ("skill:fs/config.yaml", "key: value"),
        ],
    )
    async def test_resolve_content_skill_ref_skips_mime_and_wraps(
        self, mock_storage, resolver, ref, content,
    ):
        resolver.read.return_value = content
        svc = FileConversionService(storage=mock_storage, skill_files=resolver)

        out = await svc.resolve_content(ref, "u1")

        assert out == f"[File: {ref}]\n{content}\n[/File: {ref}]"
        resolver.read.assert_awaited_once_with("u1", ref)
        mock_storage.download.assert_not_called()


class TestResolveBytesSkillRef:

    async def test_resolve_bytes_skill_ref_returns_utf8(self, mock_storage, resolver):
        resolver.read.return_value = "hello"
        svc = FileConversionService(storage=mock_storage, skill_files=resolver)

        out = await svc.resolve_bytes("skill:fs/r.md", "u1")

        assert out == b"hello"
        resolver.read.assert_awaited_once_with("u1", "skill:fs/r.md")
        mock_storage.download.assert_not_called()


class TestNoResolverConfigured:

    async def test_skill_ref_without_resolver_raises_file_not_found(self, mock_storage):
        svc = FileConversionService(storage=mock_storage, skill_files=None)

        with pytest.raises(FileNotFoundError):
            await svc.resolve_content("skill:fs/r.md", "u1")

        with pytest.raises(FileNotFoundError):
            await svc.resolve_bytes("skill:fs/r.md", "u1")

        mock_storage.download.assert_not_called()


class TestBareAndDeliveredRefsUnchanged:

    async def test_bare_ref_still_uses_storage_download(self, mock_storage, resolver):
        mock_storage.download = AsyncMock(return_value=b"hi\n")
        svc = FileConversionService(storage=mock_storage, skill_files=resolver)

        out = await svc.resolve_bytes("report.txt", "u1")

        assert out == b"hi\n"
        mock_storage.download.assert_awaited_once_with("report.txt", "u1")
        resolver.read.assert_not_awaited()

    async def test_delivered_ref_still_uses_media_storage(self, mock_storage, resolver):
        media_storage = AsyncMock()
        media_storage.fetch = AsyncMock(return_value=b"doc bytes")
        svc = FileConversionService(
            storage=mock_storage, media_storage=media_storage, skill_files=resolver,
        )

        out = await svc.resolve_bytes("docs/u1/report.html", "u1")

        assert out == b"doc bytes"
        media_storage.fetch.assert_awaited_once_with("docs/u1/report.html")
        resolver.read.assert_not_awaited()
        mock_storage.download.assert_not_called()
