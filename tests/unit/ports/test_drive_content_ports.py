"""Port contracts added by Slice 2+4 (USER_DRIVE_INDEXING_RFC §4, §9, §24.3, §24.5, §24.6): the extractor, the image
triage, the repository's chunk methods and the drive's content reads. Names, async-ness and parameters are pinned:
the plan parts are written against them."""
import inspect

import pytest

from src.domain.user_drive import DriveFileTooLarge
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.image_triage_port import ImageTriagePort
from src.ports.user_drive_port import UserDrivePort


def params(cls, name):
    signature = inspect.signature(getattr(cls, name))
    return [p for p in signature.parameters if p != "self"], {
        p.name: p.default for p in signature.parameters.values() if p.default is not inspect.Parameter.empty}


class TestContentExtractorPort:
    def test_abstract_surface(self):
        assert set(ContentExtractorPort.__abstractmethods__) == {"extractor_id", "version", "extract"}
        with pytest.raises(TypeError):
            ContentExtractorPort()  # type: ignore[abstract]

    @pytest.mark.parametrize("name", ["extractor_id", "version"])
    def test_identity_is_a_property(self, name):
        assert isinstance(inspect.getattr_static(ContentExtractorPort, name), property)

    def test_extract(self):
        assert inspect.iscoroutinefunction(ContentExtractorPort.extract)
        assert params(ContentExtractorPort, "extract") == (["request"], {})


class TestImageTriagePort:
    def test_abstract_surface(self):
        assert set(ImageTriagePort.__abstractmethods__) == {"triage"}

    def test_triage(self):
        assert inspect.iscoroutinefunction(ImageTriagePort.triage)
        assert params(ImageTriagePort, "triage") == (["user_id", "account_id", "images"], {})


_CHUNK_METHODS = {   # connection-scoped: a ConnectionScope where the user id was (MULTI_DRIVE_RFC §8)
    "write_chunks": (["chunks", "vectors"], {}),
    "delete_chunks_except": (["scope", "item_id", "keep_generation"], {}),
    "find_nearest_chunks": (["scope", "vector", "limit", "kind"], {"kind": None}),
    "find_chunks_by_term": (["scope", "term", "limit", "kind"], {"kind": None}),
    "get_chunks": (["scope", "item_id", "generation", "indexes"], {}),
    "readable_chunk_total": (["scope"], {}),
    "chunks_page": (["scope", "after", "limit"], {}),
    "delete_user_chunks": (["scope", "limit"], {}),
}


class TestRepositoryChunkMethods:
    def test_abstract(self):
        assert set(_CHUNK_METHODS) <= set(DriveIndexRepository.__abstractmethods__)

    @pytest.mark.parametrize("name", sorted(_CHUNK_METHODS))
    def test_async_with_pinned_parameters(self, name):
        assert inspect.iscoroutinefunction(getattr(DriveIndexRepository, name))
        assert params(DriveIndexRepository, name) == _CHUNK_METHODS[name]


_DRIVE_CONTENT = {   # by handle; a thumbnail asks for its longest edge in px (MULTI_DRIVE_RFC §6)
    "download_to_file": (["handle", "item_id", "dest_path", "max_bytes"], {}),
    "get_thumbnail": (["handle", "item_id", "max_edge_px"], {}),
}


class TestUserDriveContentReads:
    def test_abstract(self):
        assert set(_DRIVE_CONTENT) <= set(UserDrivePort.__abstractmethods__)

    @pytest.mark.parametrize("name", sorted(_DRIVE_CONTENT))
    def test_async_with_pinned_parameters(self, name):
        assert inspect.iscoroutinefunction(getattr(UserDrivePort, name))
        assert params(UserDrivePort, name) == _DRIVE_CONTENT[name]

    def test_too_large_carries_its_limit(self):
        err = DriveFileTooLarge(10 * 1024 * 1024)
        assert err.limit_bytes == 10 * 1024 * 1024 and "10.0MB" in str(err)

    def test_every_drive_implementation_has_the_content_reads(self):
        from scripts.validation.drive_eval.fixture_drive import FixtureDrive
        from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
        for cls in (OneDriveAdapter, FixtureDrive):
            assert not ({"download_to_file", "get_thumbnail"} & set(cls.__abstractmethods__)), cls
