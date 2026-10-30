"""
Spike S3 (USER_DRIVE_INDEXING_RFC §18): cost and latency of the chunk kNN at 10k and 50k chunks under a user_id
pre-filter, against live Firestore. Owner-run, after the chunk vector indexes are READY.

Writes synthetic chunks for one `zz-synthetic-…` user (random unit vectors, 2 400-character texts, 80 % pdf / 20 %
docx), grows them to each size in turn, and at each size times `find_nearest_chunks` at CHUNK_FETCH and FETCH_MAX,
with and without the kind pre-filter. The first query of each configuration also runs with explain-analyze, which
reports the billed read operations. Whatever happens, the user's chunks are purged at the end (`--keep` skips it).
The user has no records, so nothing else of the index is touched. `--purge-only` deletes a synthetic user's chunks
left by an interrupted run, and measures nothing.

It confirms (or not) two parameters: MAX_CHUNKS_PER_USER (50 000: the kNN cost at that size) and CHUNK_FETCH (300) /
FETCH_MAX (1 000: the page and the widened page). The verdict compares p95 latencies with the budgets below; the
read cost is printed for the owner to price at the database location's read price.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_chunk_knn_cost.py zz-synthetic-s3-20261013 [--sizes 10000,50000]
        [--queries 20] [--read-price-per-100k 0.03] [--keep | --purge-only]
Exit code: 0 when the caps are confirmed, 2 when a budget is exceeded, 1 on a refusal or an error.
"""
import argparse
import asyncio
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_index import DriveChunk  # noqa: E402
from src.domain.drive_index_params import CHUNK_FETCH, FETCH_MAX, RECORD_WRITE_BATCH  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

SYNTHETIC_PREFIX = "zz-synthetic-"
DIMENSIONS = 768
CHUNKS_PER_ITEM = 100
TEXT = ("Synthetic chunk text for the S3 spike. " * 62)[:2400]
# p95 budgets per kNN call: the search runs one kNN per stream per member kind, in parallel with the others
BUDGET_MS = {CHUNK_FETCH: 1500.0, FETCH_MAX: 3000.0}

Explain = Callable[[str, Sequence[float], int, Optional[str]], Awaitable[Optional[int]]]


@dataclass(frozen=True)
class Row:
    size: int
    limit: int
    kind: Optional[str]
    p50_ms: float
    p95_ms: float
    read_ops: Optional[int]


def require_synthetic(user_id: str) -> None:
    if not user_id.startswith(SYNTHETIC_PREFIX) or len(user_id) <= len(SYNTHETIC_PREFIX):
        raise ValueError(f"refusing user {user_id!r}: the spike writes only for a {SYNTHETIC_PREFIX}… user")


S3_CONNECTION = "s3-conn"   # the made-up connection the spike's chunks belong to; the synthetic user has no real one


def scope_of(user_id: str) -> ConnectionScope:
    """The scope of the synthetic user's chunks: the made-up connection `S3_CONNECTION` (MULTI_DRIVE_RFC §8)."""
    return ConnectionScope(user_id, S3_CONNECTION)


def unit_vector(rng: random.Random, dims: int = DIMENSIONS) -> List[float]:
    raw = [rng.gauss(0.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def synthetic_chunks(user_id: str, start: int, stop: int) -> List[DriveChunk]:
    """Chunks number `start`..`stop - 1`: CHUNKS_PER_ITEM per item, every fifth item a docx."""
    out = []
    for n in range(start, stop):
        item = n // CHUNKS_PER_ITEM
        out.append(DriveChunk(user_id=user_id, item_id=f"s3-item-{item:05d}", generation=1,
                              index=n % CHUNKS_PER_ITEM, text=TEXT, char_start=0, char_end=len(TEXT),
                              kind="docx" if item % 5 == 4 else "pdf", connection_id=S3_CONNECTION))
    return out


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[rank]


def verdict(rows: Sequence[Row], largest: int) -> List[str]:
    """Budget breaches at the largest size; empty when the caps are confirmed."""
    return [f"p95 {r.p95_ms:.0f} ms > {BUDGET_MS[r.limit]:.0f} ms at {r.size} chunks, limit {r.limit}, "
            f"kind {r.kind or 'any'}" for r in rows if r.size == largest and r.p95_ms > BUDGET_MS.get(r.limit, math.inf)]


async def grow(repo: DriveIndexRepository, user_id: str, have: int, want: int, rng: random.Random) -> int:
    for start in range(have, want, RECORD_WRITE_BATCH):
        chunks = synthetic_chunks(user_id, start, min(want, start + RECORD_WRITE_BATCH))
        await repo.write_chunks(chunks, [unit_vector(rng) for _ in chunks])
    return max(have, want)


async def measure(repo: DriveIndexRepository, user_id: str, size: int, queries: int, rng: random.Random,
                  explain: Optional[Explain] = None,
                  clock: Callable[[], float] = time.perf_counter) -> List[Row]:
    rows = []
    for limit in (CHUNK_FETCH, FETCH_MAX):
        for kind in (None, "pdf"):
            read_ops = await explain(user_id, unit_vector(rng), limit, kind) if explain else None
            timings = []
            for _ in range(queries):
                vector = unit_vector(rng)
                started = clock()
                await repo.find_nearest_chunks(scope_of(user_id), vector, limit, kind)
                timings.append((clock() - started) * 1000)
            rows.append(Row(size, limit, kind, percentile(timings, 0.5), percentile(timings, 0.95), read_ops))
    return rows


async def purge(repo: DriveIndexRepository, user_id: str) -> int:
    total = 0
    while (deleted := await repo.delete_user_chunks(scope_of(user_id), RECORD_WRITE_BATCH)) > 0:
        total += deleted
    return total


async def run(repo: DriveIndexRepository, user_id: str, sizes: Sequence[int], queries: int, *, seed: int = 3,
              explain: Optional[Explain] = None, keep: bool = False,
              clock: Callable[[], float] = time.perf_counter) -> List[Row]:
    require_synthetic(user_id)
    if await repo.chunks_page(scope_of(user_id), None, 1):
        raise ValueError(f"{user_id} already has chunks: purge them first or pick a new synthetic id")
    rng = random.Random(seed)
    rows: List[Row] = []
    have = 0
    try:
        for size in sorted(sizes):
            have = await grow(repo, user_id, have, size, rng)
            rows.extend(await measure(repo, user_id, size, queries, rng, explain, clock))
    finally:
        if not keep:
            print(f"🧹 purged {await purge(repo, user_id)} synthetic chunks")
    return rows


async def purge_only(repo: DriveIndexRepository, user_id: str) -> int:
    try:
        require_synthetic(user_id)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    print(f"🧹 purged {await purge(repo, user_id)} synthetic chunks")
    return 0


def report(rows: Sequence[Row], read_price_per_100k: float) -> str:
    lines = ["size     limit  kind  p50_ms  p95_ms  read_ops  $/1000 queries"]
    for r in rows:
        cost = f"{r.read_ops * read_price_per_100k / 100_000 * 1000:.4f}" if r.read_ops is not None else "-"
        lines.append(f"{r.size:<8} {r.limit:<6} {r.kind or 'any':<5} {r.p50_ms:>6.0f}  {r.p95_ms:>6.0f}  "
                     f"{r.read_ops if r.read_ops is not None else '-':>8}  {cost}")
    return "\n".join(lines)


def firestore_explain(repo: Any) -> Explain:
    from google.cloud.firestore_v1.query_profile import ExplainOptions

    async def explain(user_id: str, vector: Sequence[float], limit: int, kind: Optional[str]) -> Optional[int]:
        results = await repo.nearest_chunks_query(scope_of(user_id), vector, limit, kind).get(
            explain_options=ExplainOptions(analyze=True))
        return int(results.get_explain_metrics().execution_stats.read_operations)

    return explain


async def _main(args: argparse.Namespace) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    if args.purge_only:
        return await purge_only(repo, args.user_id)
    sizes = [int(s) for s in args.sizes.split(",")]
    try:
        rows = await run(repo, args.user_id, sizes, args.queries, explain=firestore_explain(repo), keep=args.keep)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    print(report(rows, args.read_price_per_100k))
    breaches = verdict(rows, max(sizes))
    for line in breaches:
        print(f"⚠️  {line}")
    print("✅ MAX_CHUNKS_PER_USER and CHUNK_FETCH / FETCH_MAX confirmed" if not breaches
          else "❌ revisit MAX_CHUNKS_PER_USER or CHUNK_FETCH (§15) before the slice ships")
    return 0 if not breaches else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("user_id", help=f"a {SYNTHETIC_PREFIX}… id; its chunks are written, then purged")
    parser.add_argument("--sizes", default="10000,50000", help="chunk counts to measure at, ascending")
    parser.add_argument("--queries", type=int, default=20, help="timed kNN calls per configuration")
    parser.add_argument("--read-price-per-100k", type=float, default=0.03,
                        help="USD per 100 000 reads at the database location (check the current price)")
    parser.add_argument("--keep", action="store_true", help="do not purge the synthetic chunks")
    parser.add_argument("--purge-only", action="store_true", help="only delete the user's synthetic chunks")
    sys.exit(asyncio.run(_main(parser.parse_args())))
