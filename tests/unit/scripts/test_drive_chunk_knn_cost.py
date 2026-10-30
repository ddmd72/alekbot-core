"""scripts/infrastructure/drive_chunk_knn_cost.py (spike S3, USER_DRIVE_INDEXING_RFC §18): writes only for a
`zz-synthetic-…` user, grows to each size, times every configuration, always purges, and reports a verdict against
the budgets. Run here on the in-memory repository at small sizes, never on the network."""
import importlib.util
import itertools
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index_params import CHUNK_FETCH, FETCH_MAX

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_chunk_knn_cost.py"


@pytest.fixture
def s3():
    spec = importlib.util.spec_from_file_location("drive_chunk_knn_cost", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("user_id", ["owner-123", "zz-synthetic-", "zz-real-user"])
async def test_refuses_a_user_that_is_not_synthetic(s3, user_id):
    repo = InMemoryDriveIndexRepository()
    with pytest.raises(ValueError):
        await s3.run(repo, user_id, [10], 1)
    assert repo.chunks == {}


async def test_refuses_a_synthetic_user_that_already_has_chunks(s3):
    repo = InMemoryDriveIndexRepository()
    await repo.write_chunks(s3.synthetic_chunks("zz-synthetic-x", 0, 1), [[1.0]])
    with pytest.raises(ValueError):
        await s3.run(repo, "zz-synthetic-x", [10], 1)
    assert len(repo.chunks) == 1   # left as found


@pytest.mark.slow
async def test_grows_measures_every_configuration_and_purges(s3):
    repo = InMemoryDriveIndexRepository()
    sizes_seen = []

    async def explain(user_id, vector, limit, kind):
        sizes_seen.append(len(repo.chunks))
        return 7

    ticks = itertools.count(step=0.002)
    rows = await s3.run(repo, "zz-synthetic-s3", [250, 120], 3, explain=explain, clock=lambda: next(ticks))
    assert [(r.size, r.limit, r.kind) for r in rows] == [
        (size, limit, kind) for size in (120, 250) for limit in (CHUNK_FETCH, FETCH_MAX) for kind in (None, "pdf")]
    assert sizes_seen == [120] * 4 + [250] * 4
    assert all(r.read_ops == 7 and r.p95_ms == pytest.approx(2.0) for r in rows)
    assert repo.chunks == {}


@pytest.mark.slow
async def test_synthetic_chunks_are_unit_vectors_of_both_kinds(s3):
    repo = InMemoryDriveIndexRepository()
    await s3.run(repo, "zz-synthetic-s3", [500], 1, keep=True)
    assert len(repo.chunks) == 500
    kinds = {c.kind for c, _ in repo.chunks.values()}
    assert kinds == {"pdf", "docx"}
    _, vector = next(iter(repo.chunks.values()))
    assert len(vector) == s3.DIMENSIONS and sum(x * x for x in vector) == pytest.approx(1.0)


async def test_purges_even_when_a_measurement_fails(s3):
    class Failing(InMemoryDriveIndexRepository):
        async def find_nearest_chunks(self, *args, **kwargs):
            raise RuntimeError("index not ready")

    repo = Failing()
    with pytest.raises(RuntimeError):
        await s3.run(repo, "zz-synthetic-s3", [50], 1)
    assert repo.chunks == {}


def test_verdict_and_report(s3):
    rows = [s3.Row(50_000, CHUNK_FETCH, None, 400.0, 900.0, 520), s3.Row(50_000, FETCH_MAX, "pdf", 900, 3500, 520),
            s3.Row(10_000, FETCH_MAX, None, 300, 9_999, 110)]
    breaches = s3.verdict(rows, 50_000)
    assert len(breaches) == 1 and "limit 1000" in breaches[0] and "kind pdf" in breaches[0]
    assert s3.verdict(rows[:1], 50_000) == []
    text = s3.report(rows, 0.03)
    assert "50000" in text and "0.1560" in text   # 520 reads x $0.03 / 100k x 1000 queries


def test_percentile(s3):
    assert s3.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.95) == 5.0
    assert s3.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert s3.percentile([], 0.5) == 0.0


async def test_firestore_explain_reads_the_billed_reads_of_the_repository_query(s3):
    from types import SimpleNamespace

    calls = []

    class Query:
        async def get(self, **kwargs):
            calls.append(kwargs["explain_options"].analyze)
            stats = SimpleNamespace(read_operations=512)
            return SimpleNamespace(get_explain_metrics=lambda: SimpleNamespace(execution_stats=stats))

    repo = SimpleNamespace(nearest_chunks_query=lambda user_id, vector, limit, kind: Query())
    assert await s3.firestore_explain(repo)("zz-synthetic-s3", [1.0], 300, None) == 512
    assert calls == [True]


async def test_purge_only_deletes_a_synthetic_users_leftovers_and_refuses_a_real_one(s3):
    repo = InMemoryDriveIndexRepository()
    await repo.write_chunks(s3.synthetic_chunks("zz-synthetic-s3", 0, 450), [[1.0]] * 450)
    await repo.write_chunks(s3.synthetic_chunks("owner-1", 0, 1), [[1.0]])
    assert await s3.purge_only(repo, "owner-1") == 1
    assert await s3.purge_only(repo, "zz-synthetic-s3") == 0
    # chunks are keyed by connection (MULTI_DRIVE_RFC §8): the spike's made-up connection; the one left is owner-1's
    assert [(k[0], chunk.user_id) for k, (chunk, _) in repo.chunks.items()] == [("s3-conn", "owner-1")]
