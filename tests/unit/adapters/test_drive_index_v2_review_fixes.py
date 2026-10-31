"""Search v2 P2 review fixes (USER_DRIVE_INDEXING_RFC §25.13.1; arbiter rulings on review rv_sv2_p2):
- M1: a key list of more than CONCEPT_KEYS_MAX distinct keys is refused (logged, ValueError) by both adapters, never
  cut: a cut key is a file lost without a trace;
- m1: initial-build spends in one process are serialised, so concurrent spends never lose one;
- m3: the sweep's chunk page reads ids, never text or keys;
- m4: the evaluating fake refuses what Firestore refuses (over 30 values, two array filters, a disjunction product
  over 30, a non-equality kNN pre-filter);
- m7: the smoke script's count labels follow the job and state enums."""
import asyncio
import importlib.util
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from google.api_core.exceptions import InvalidArgument
from google.cloud.firestore import FieldFilter

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import EnrichJob, JobState
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import CONCEPT_KEYS_MAX, INITIAL_BUILD_USD

from tests.unit.adapters.firestore_drive_eval_fakes import (
    CHUNKS, RECS, EvalDb, EvalDocRef, eval_transactions, make_eval_repo,
)
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 13, tzinfo=timezone.utc)
TOO_MANY = [f"w:k{i}" for i in range(CONCEPT_KEYS_MAX + 1)]
REALM, SUBJECT = "microsoft", "acct-1"   # the provider account an initial build is keyed by (MULTI_DRIVE_RFC §10)
KEY_READS = (
    ("find_by_about_keys", lambda repo, keys: repo.find_by_about_keys(U_SCOPE, keys, 5)),
    ("find_expiring", lambda repo, keys: repo.find_expiring(U_SCOPE, keys, date(2026, 10, 13), 5)),
    ("find_chunks_by_word_keys", lambda repo, keys: repo.find_chunks_by_word_keys(U_SCOPE, keys, 5)),
    ("find_hints_by_keys", lambda repo, keys: repo.find_hints_by_keys(U_SCOPE, keys, 5)),
)


@pytest.fixture
def adapters(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield db, [InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)]


class TestKeyLimit:
    @pytest.mark.parametrize("name,read", KEY_READS, ids=[n for n, _ in KEY_READS])
    async def test_more_than_the_limit_is_refused_and_logged(self, adapters, caplog, name, read):
        db, repos = adapters
        for repo in repos:
            caplog.clear()
            with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match=str(CONCEPT_KEYS_MAX)):
                await read(repo, TOO_MANY)
            assert any(r.levelno == logging.ERROR and name in r.getMessage() for r in caplog.records), name
        assert all(not col.executed for col in db.cols.values())   # refused before any query

    @pytest.mark.parametrize("name,read", KEY_READS, ids=[n for n, _ in KEY_READS])
    async def test_the_limit_counts_distinct_keys(self, adapters, name, read):
        _, repos = adapters
        keys = [f"w:k{i}" for i in range(CONCEPT_KEYS_MAX)] + ["w:k0", "w:k1"]
        for repo in repos:
            assert await read(repo, keys) == []


class TestInitialBuildLock:
    async def test_concurrent_spends_never_lose_one(self, mock_env_config, monkeypatch):
        real_get = EvalDocRef.get

        async def yielding_get(self, *args, **kwargs):
            snap = await real_get(self, *args, **kwargs)
            await asyncio.sleep(0)   # a real read suspends: another spend may run in between
            return snap

        monkeypatch.setattr(EvalDocRef, "get", yielding_get)
        with eval_transactions():
            repo = make_eval_repo(EvalDb(), mock_env_config)
            covered = await asyncio.gather(*(repo.spend_initial_build("u", REALM, SUBJECT, 7.0, T) for _ in range(8)))
            build = await repo.get_initial_build("u", REALM, SUBJECT)
        assert sum(c for _, c in covered) == INITIAL_BUILD_USD == build.spent_usd
        assert build.closed_at == T


class TestChunksPageReadsIds:
    async def test_no_text_or_keys(self, adapters):
        db, (_, repo) = adapters
        chunk = DriveChunk("u", "a", 1, 0, "long text", 0, 9, "pdf", ("1250",), ("w:x",) * 3, 1, connection_id="u")
        await repo.write_chunks([chunk], [[1.0, 0.0]])
        (page,) = await repo.chunks_page(U_SCOPE, None, 5)
        (ops,) = db.cols[CHUNKS].executed
        selected = next(op[1] for op in ops if op[0] == "select")
        assert not {"text", "word_keys", "content_terms", "vector"} & set(selected)
        assert (page.item_id, page.generation, page.index, page.text, page.word_keys) == ("a", 1, 0, "", ())


class TestTheFakeEnforcesFirestoreLimits:
    async def run(self, *filters, nearest=False):
        col = EvalDb().collection(RECS)
        query = col.where(filter=FieldFilter("user_id", "==", "u"))
        for f in filters:
            query = query.where(filter=f)
        if nearest:
            query = query.find_nearest(vector_field="v", query_vector=[1.0], limit=5, distance_result_field="d")
        return await query.get()

    async def test_more_than_thirty_values(self):
        with pytest.raises(InvalidArgument):
            await self.run(FieldFilter("about_keys", "array_contains_any", TOO_MANY))
        with pytest.raises(InvalidArgument):
            await self.run(FieldFilter("kind", "in", TOO_MANY))

    async def test_two_array_filters(self):
        with pytest.raises(InvalidArgument):
            await self.run(FieldFilter("a", "array_contains", "x"), FieldFilter("b", "array_contains_any", ["y"]))

    async def test_a_disjunction_product_over_thirty(self):
        with pytest.raises(InvalidArgument):
            await self.run(FieldFilter("kind", "in", [f"k{i}" for i in range(6)]),
                           FieldFilter("about_keys", "array_contains_any", [f"e:{i}" for i in range(6)]))
        assert await self.run(FieldFilter("kind", "in", ["a", "b"]),
                              FieldFilter("about_keys", "array_contains_any", [f"e:{i}" for i in range(15)])) == []

    async def test_a_knn_pre_filter_is_equality_only(self):
        with pytest.raises(InvalidArgument):
            await self.run(FieldFilter("effective_date", ">=", T), nearest=True)
        assert await self.run(FieldFilter("kind", "==", "pdf"), nearest=True) == []


def test_the_smoke_count_labels_follow_the_enums(mock_env_config, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "drive_index_smoke", Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.setattr(smoke, "SLICE2_CONTENT_KINDS", frozenset(f"kind{i:02d}" for i in range(35)))
    names = [n for n, _ in smoke.checks(make_eval_repo(EvalDb(), mock_env_config), "u")
             if n.startswith("count_by_job_state[")]
    per_batch = len(EnrichJob) * len(JobState)
    assert len(names) == 2 * per_batch
    assert sum(n.endswith("batch 0]") for n in names) == per_batch == sum(n.endswith("batch 1]") for n in names)
