"""
An on-disk cache of the built fixture index, for the drive evals that run a real model on it (G3 `smart_dry_run`, the
drive specialist eval `specialist_ab` and its replay, DRIVE_SPECIALIST_RFC §12): the index build costs ~30 minutes
and many embedding calls, and its inputs rarely change. A cached index also makes a before/after pair measure the same
index.

The key is a hash of everything the build reads: the tree manifests, every byte the fixture drive serves (files,
renditions, rendered templates), the fixture text modules (templates and hand-written bulk profiles), the enrichment
snapshot, every `*_VERSION` of src/domain/drive_index.py plus the reader's, and the embedding space. A run whose key
matches loads the index (the repository's stores and the eval clock) instead of rebuilding it; `--rebuild-index`
forces a build, which then refreshes the cache. A build with enrichment misses is never cached. Stored under the
gitignored scripts/memory/drive_g3/cache/ as a pickle of our own data (never load a cache file you did not write).
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository

CACHE_FORMAT = 1
_HERE = Path(__file__).resolve().parent
_CACHE_DIR = _HERE.parents[2] / "scripts" / "memory" / "drive_g3" / "cache"
_STORES = ("records", "vectors", "states", "chunks", "hints", "usage", "initial", "transcript_jobs",
           "window_results", "batches", "transcripts")
_CODE = ("fixture_texts.py", "fixture_texts_v2.py", "fixture_texts_s26.py", "fixture_drive.py", "search_v2_eval.py",
         "enrichment_replay.py")


def code_versions() -> Dict[str, int]:
    from src.domain import drive_index

    from .search_v2_eval import READER_VERSION
    versions = {name: value for name, value in vars(drive_index).items()
                if name.endswith("_VERSION") and isinstance(value, int)}
    versions["READER_VERSION"] = int(READER_VERSION)
    return dict(sorted(versions.items()))


def cache_key(trees: Sequence[Path], snapshot: Path, drive: Any, embedding_space: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"format:{CACHE_FORMAT}\n".encode())
    for path in trees:
        digest.update(f"tree:{path.name}:".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(f"content:{drive.content_sha256()}\n".encode())
    for name in _CODE:
        source = _HERE / name
        if source.exists():
            digest.update(f"code:{name}:".encode())
            digest.update(hashlib.sha256(source.read_bytes()).digest())
    digest.update(f"snapshot:{hashlib.sha256(snapshot.read_bytes()).hexdigest()}\n".encode())
    digest.update(json.dumps(code_versions(), sort_keys=True).encode())
    digest.update(f"space:{embedding_space}\n".encode())
    return digest.hexdigest()[:32]


def dump(repo: InMemoryDriveIndexRepository, clock: Any, facts: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": CACHE_FORMAT, "stores": {name: getattr(repo, name) for name in _STORES},
               "clock_now": clock.now, "facts": facts}
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load(path: Path, clock: Any) -> Tuple[InMemoryDriveIndexRepository, Dict[str, Any]]:
    with path.open("rb") as fh:
        payload = pickle.load(fh)   # noqa: S301 — our own cache file, written by dump()
    if payload.get("format") != CACHE_FORMAT:
        raise ValueError("cache format changed")
    repo = InMemoryDriveIndexRepository()
    for name in _STORES:
        setattr(repo, name, payload["stores"][name])
    clock.now = payload["clock_now"]
    return repo, payload["facts"]


Build = Callable[[], Awaitable[Tuple[InMemoryDriveIndexRepository, Dict[str, Any]]]]


async def load_or_build(build: Build, *, trees: Sequence[Path], snapshot: Path, drive: Any, embedding_space: str,
                        clock: Any, rebuild: bool = False, cache_dir: Optional[Path] = None
                        ) -> Tuple[InMemoryDriveIndexRepository, Dict[str, Any]]:
    """The cached index when the key matches (and no rebuild is asked); otherwise build() and store a clean build
    (one without enrichment misses)."""
    key = cache_key(trees, snapshot, drive, embedding_space)
    path = (cache_dir or _CACHE_DIR) / f"index_{key}.pickle"
    if path.exists() and not rebuild:
        try:
            repo, facts = load(path, clock)
            print(f"index: loaded from cache {path.name}")
            return repo, {**facts, "cache": "hit"}
        except Exception as exc:   # noqa: BLE001 — an unreadable cache is rebuilt, never trusted
            print(f"index: cache {path.name} unreadable ({type(exc).__name__}); rebuilding")
    repo, facts = await build()
    if not facts.get("misses"):
        dump(repo, clock, facts, path)
        print(f"index: built and cached as {path.name}")
    return repo, {**facts, "cache": "rebuilt" if rebuild else "miss"}
