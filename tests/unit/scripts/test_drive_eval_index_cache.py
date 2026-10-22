"""The drive evals' index cache (scripts/validation/drive_eval/index_cache.py, DRIVE_SPECIALIST_RFC §12): a matching
key loads the built index, a changed input or --rebuild-index builds it again, a build with enrichment misses is never
stored, and an unreadable cache file is rebuilt, never trusted."""
import json
from typing import List

from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from scripts.validation.drive_eval.index_cache import cache_key, load_or_build
from scripts.validation.drive_eval.runner import EvalClock, _EnabledUsers, index_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_search import DriveSearchRequest
from src.services.drive_search_service import DriveSearchService

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import MANIFEST, fake_content, rome


def _inputs(tmp_path):
    tree, snapshot = tmp_path / "tree.json", tmp_path / "snapshot.json"
    tree.write_text(json.dumps(MANIFEST), encoding="utf-8")
    snapshot.write_text("{}", encoding="utf-8")
    return tree, snapshot


def _builder(drive, clock, builds: List[int], misses=None):
    async def build():
        builds.append(1)
        repo = InMemoryDriveIndexRepository()
        await index_fixture(drive, repo, FakeEmbeddings(), rome, "fake/8", clock, throttle_pause_s=0, **fake_content())
        return repo, {"misses": misses or {}}
    return build


async def _found(drive, repo, clock):
    search = DriveSearchService(repo, FakeEmbeddings(), _EnabledUsers(), drive, clock=clock, embedding_space="fake/8")
    result = await search.search("eval-user", DriveSearchRequest(query="Scan_0012"))
    return sorted(c.path for c in result.cards), clock.now


async def test_a_matching_key_loads_the_build_and_rebuild_builds_again(tmp_path):
    drive = FixtureDrive(MANIFEST)
    tree, snapshot = _inputs(tmp_path)
    kwargs = dict(trees=(tree,), snapshot=snapshot, drive=drive, embedding_space="fake/8", cache_dir=tmp_path)
    builds: List[int] = []
    clock1, clock2, clock3 = EvalClock(), EvalClock(), EvalClock()
    repo1, facts1 = await load_or_build(_builder(drive, clock1, builds), clock=clock1, **kwargs)
    repo2, facts2 = await load_or_build(_builder(drive, clock2, builds), clock=clock2, **kwargs)
    assert builds == [1] and facts1["cache"] == "miss" and facts2["cache"] == "hit"
    found = await _found(drive, repo2, clock2)
    assert found == await _found(drive, repo1, clock1) and "Scans/Scan_0012.pdf" in found[0]
    _, facts3 = await load_or_build(_builder(drive, clock3, builds), clock=clock3, rebuild=True, **kwargs)
    assert builds == [1, 1] and facts3["cache"] == "rebuilt"


async def test_a_changed_input_is_another_key_and_a_build(tmp_path):
    drive = FixtureDrive(MANIFEST)
    tree, snapshot = _inputs(tmp_path)
    before = cache_key((tree,), snapshot, drive, "fake/8")
    snapshot.write_text('{"changed": 1}', encoding="utf-8")
    assert cache_key((tree,), snapshot, drive, "fake/8") != before
    assert cache_key((tree,), snapshot, drive, "fake/16") != cache_key((tree,), snapshot, drive, "fake/8")


async def test_a_build_with_misses_is_never_cached(tmp_path):
    drive = FixtureDrive(MANIFEST)
    tree, snapshot = _inputs(tmp_path)
    kwargs = dict(trees=(tree,), snapshot=snapshot, drive=drive, embedding_space="fake/8", cache_dir=tmp_path)
    builds: List[int] = []
    await load_or_build(_builder(drive, EvalClock(), builds, misses={"profile": 1}), clock=EvalClock(), **kwargs)
    await load_or_build(_builder(drive, EvalClock(), builds), clock=EvalClock(), **kwargs)
    assert builds == [1, 1]
    assert not list(tmp_path.glob("*.tmp"))


async def test_an_unreadable_cache_file_is_rebuilt(tmp_path):
    drive = FixtureDrive(MANIFEST)
    tree, snapshot = _inputs(tmp_path)
    kwargs = dict(trees=(tree,), snapshot=snapshot, drive=drive, embedding_space="fake/8", cache_dir=tmp_path)
    builds: List[int] = []
    await load_or_build(_builder(drive, EvalClock(), builds), clock=EvalClock(), **kwargs)
    (cached,) = tmp_path.glob("index_*.pickle")
    cached.write_bytes(b"not a pickle")
    _, facts = await load_or_build(_builder(drive, EvalClock(), builds), clock=EvalClock(), **kwargs)
    assert builds == [1, 1] and facts["cache"] == "miss"
