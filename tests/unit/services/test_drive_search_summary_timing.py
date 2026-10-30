"""The one [DriveSearch] summary line says how long the search took (USER_DRIVE_INDEXING_RFC §10.1, §12.1)."""
import itertools
import re
from unittest.mock import MagicMock

from src.services import drive_search_service as search_mod
from src.services.drive_search_service import DriveSearchService

from .drive_harness_connections import harness_connections
from .test_drive_search_service import World


async def test_the_summary_line_carries_the_elapsed_milliseconds(monkeypatch):
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    ticks = itertools.count(start=10.0, step=0.25)   # each reading 250 ms after the previous one
    svc = DriveSearchService(w.repo, w.emb, w.drive, harness_connections(w.users, w.drive), clock=w.clock,
                             nonce=lambda: "nonce0001", monotonic=lambda: next(ticks), embedding_space="test-space/8")
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    from src.domain.drive_search import DriveSearchRequest
    await svc.search("u", DriveSearchRequest(query="a"))
    line = log.info.call_args[0][0]
    assert re.search(r"\bms=250\b", line), line
