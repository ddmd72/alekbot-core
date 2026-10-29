"""The document child gets the same RLIMIT_NPROC as the media child, only when sandboxed (hardening, M3)."""
import os

import pytest

from src.adapters import subprocess_content_extractor as module
from src.adapters.drive_parser.media_extractor import MEDIA_NPROC_LIMIT
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import ExtractionRequest


def test_the_document_child_limit_is_the_media_childs():
    assert module.EXTRACT_NPROC_LIMIT == MEDIA_NPROC_LIMIT


async def test_the_limit_is_requested_only_for_the_unprivileged_user(tmp_path, monkeypatch):
    requests = []

    async def spawn(self, request, workdir, stderr):
        requests.append(dict(request))
        return 0, False
    monkeypatch.setattr(SubprocessContentExtractor, "_spawn", spawn)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    source = tmp_path / "a.txt"
    source.write_bytes(b"x")
    work = tmp_path / "work"
    work.mkdir()
    for sandbox in (None, (1234, 1234)):
        extractor = SubprocessContentExtractor(temp_root=str(work))
        extractor._sandbox = sandbox
        await extractor.extract(ExtractionRequest(path=str(source), name="a.txt", kind="text", mime_type="",
                                                  user_id="u1", account_id="a1"))
    assert "nproc" not in requests[0] and requests[1]["nproc"] == module.EXTRACT_NPROC_LIMIT
