"""The drive eval runner: requests as production builds them, throttled ticks, runner edge cases.
USER_DRIVE_INDEXING_RFC §17.8; RFC Δ13, Δ20."""
import json
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import runner
from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.runner import MAX_ERRORS, EvalClock, EvalError, index_fixture, request_for, run_eval
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_search import DriveSearchCard, DriveSearchResult, request_from_fields
from src.domain.exceptions import LLMRateLimitError
from src.services.drive_indexing_service import TickResult
from src.domain import drive_ref
from scripts.validation.drive_eval.eval_connection import EVAL_REF_TAG

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import GOLD, MANIFEST, fake_content, rome

LEASE_CONTRACTS = "Contracts/Lease agreement — Porto.pdf"
LEASE_INBOX = "Inbox/Lease agreement — Porto.pdf"


def _write(tmp_path, gold):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tree, gold_path = tmp_path / "tree.json", tmp_path / "gold.json"
    tree.write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return tree, gold_path, tmp_path / "bars.json", tmp_path / "report.json"


async def _eval(tmp_path, gold, embeddings=None, record=True):
    tree, gold_path, bars, report = _write(tmp_path, gold)
    return await run_eval(tree, gold_path, bars, report, embeddings=embeddings or FakeEmbeddings(),
                          embedding_space="fake/8", place_of=rome, record=record, throttle_pause_s=0,
                          **fake_content())


# ---------------------------------------------------------------------------------------------
# The request is built exactly as production builds it, and a refused gold case fails fast
# ---------------------------------------------------------------------------------------------

class TestRequestsAreBuiltAsInProduction:
    @pytest.mark.parametrize("filters", [
        {"kind": "vibes"},
        {"date_from": "2025-09-01", "date_to": "2025-08-01"},
    ], ids=["unknown-kind", "reversed-dates"])
    async def test_a_gold_case_production_would_refuse_gives_no_verdict(self, tmp_path, filters):
        embeddings = FakeEmbeddings()
        bad = {**GOLD[0], "id": "bad", "filters": filters}
        with pytest.raises(EvalError, match=r"malformed gold.*bad"):
            await _eval(tmp_path, GOLD + [bad], embeddings=embeddings)
        assert embeddings.calls == []                       # refused before indexing: no quota spent
        assert not (tmp_path / "bars.json").exists()

    @pytest.mark.parametrize("filters", [
        {"folder": "/"},
        {"kind": "spreadsheets"},
        {"kind": "PDF", "date_from": "2026-01-01"},
        {"folder": " Contracts ", "date_from": "2025-08-01", "date_to": "2025-08-31T12:00:00Z"},
    ])
    def test_request_for_matches_the_production_parser(self, filters):
        case = {"query": "  lease ", "filters": filters}
        expected = request_from_fields(case["query"], filters.get("folder"), filters.get("kind"),
                                       filters.get("date_from"), filters.get("date_to"))
        assert request_for(case) == expected

    def test_slash_folder_is_the_whole_drive(self):
        assert request_for({"query": "rome", "filters": {"folder": "/"}}).folder is None

    async def test_slash_folder_is_searchable_not_folder_not_found(self, tmp_path):
        whole = {**GOLD[0], "id": "whole", "filters": {"folder": "/"}}
        report = await _eval(tmp_path, [whole])
        assert report["cases"][0]["status"] == "ok" and report["verdict"] == "recorded"


# ---------------------------------------------------------------------------------------------
# A throttled tick waits as long as the service asked, and throttling is bounded
# ---------------------------------------------------------------------------------------------

class _Throttled(FakeEmbeddings):
    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        raise LLMRateLimitError("429 quota")


class TestThrottledTicks:
    async def _index_throttled(self, monkeypatch, pause):
        sleeps, queues = [], []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        class _Queue(runner._RecordingQueue):
            def __init__(self):
                super().__init__()
                queues.append(self)

        monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(runner, "_RecordingQueue", _Queue)
        with pytest.raises(EvalError, match=f"{runner.MAX_THROTTLED} consecutive ticks were throttled"):
            await index_fixture(FixtureDrive(MANIFEST), InMemoryDriveIndexRepository(), _Throttled(), rome,
                                "fake/8", EvalClock(), throttle_pause_s=pause, **fake_content())
        (queue,) = queues
        return sleeps, [task["delay"] for task in queue.tasks]

    async def test_the_runner_sleeps_the_delay_the_service_queued(self, monkeypatch):
        sleeps, delays = await self._index_throttled(monkeypatch, pause=30)
        assert len(delays) == runner.MAX_THROTTLED and delays[0] >= 60 and delays == sorted(delays)
        assert sleeps == delays[:-1]                 # no sleep after the tick that hits the cap

    async def test_the_pause_is_a_floor(self, monkeypatch):
        sleeps, delays = await self._index_throttled(monkeypatch, pause=100)
        assert sleeps == [max(100, d) for d in delays[:-1]] and sleeps[0] == 100

    async def test_a_tick_that_is_not_throttled_resets_the_count(self, monkeypatch):
        """Throttled ticks interleaved with progress are not consecutive: the run keeps going and settles."""
        cap = runner.MAX_THROTTLED
        script = ["throttled"] * (cap - 1) + ["done"] + ["throttled"] * (cap - 1) + ["done"]
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        class _Repo:
            settled = False

            async def get_sync_state(self, user_id):
                return SimpleNamespace(continuation_enqueued_at=None if self.settled else "pending",
                                       round_complete=self.settled)

            async def count_by_state(self, user_id):
                return {"carded": 1}

            async def oldest_due_at(self, user_id, priority):
                return None

        class _Scripted:
            def __init__(self, drive, repo, embeddings, queue, *args, **kwargs):
                self.repo, self.queue, self.calls = repo, queue, 0

            async def tick(self, user_id):
                status = script[self.calls]
                self.calls += 1
                if status == "throttled":
                    await self.queue.enqueue_worker_task("drive_sync", {}, delay_seconds=45)
                self.repo.settled = self.calls == len(script)
                return TickResult(status, {})

        monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(runner, "DriveIndexingService", _Scripted)
        out = await index_fixture(FixtureDrive(MANIFEST), _Repo(), FakeEmbeddings(), rome, "fake/8", EvalClock(),
                                  throttle_pause_s=0)
        assert out.statuses == {"throttled": 2 * (cap - 1), "done": 2} and out.ticks == len(script)
        assert sleeps == [45] * (2 * (cap - 1))


# ---------------------------------------------------------------------------------------------
# Edge cases: also_at coverage, k lower bound, the status check in `hit`, the MAX_ERRORS raise
# ---------------------------------------------------------------------------------------------

class TestRunnerGaps:
    async def test_a_duplicate_copy_hits_through_also_at(self, tmp_path):
        """Two copies of one file collapse into one card; the second copy is reachable only via `also_at`.
        The real gold's `filter-pdf-2026` depends on this."""
        both = [{"id": "pdf", "case": "filter_only", "query": "", "lang": "en", "goal": "G1",
                 "filters": {"kind": "pdf", "date_from": "2026-01-01"},
                 "expected_paths": [LEASE_CONTRACTS, LEASE_INBOX]}]
        probe = await _eval(tmp_path / "probe", both)
        groups = probe["cases"][0]["top"]
        owners = {group[0] for group in groups}
        hidden = [p for p in (LEASE_CONTRACTS, LEASE_INBOX) if p not in owners]
        assert len(hidden) == 1, groups                  # exactly one copy is folded into the other's card
        only_also_at = [{**both[0], "id": "also-at", "expected_paths": hidden}]
        report = await _eval(tmp_path / "run", only_also_at)
        row = report["cases"][0]
        assert row["hit"] and all(hidden[0] not in group[:1] for group in row["top"])

    async def test_k_zero_is_malformed(self, tmp_path):
        with pytest.raises(EvalError, match="k must be"):
            await _eval(tmp_path, GOLD + [{**GOLD[0], "id": "zero", "k": 0}])
        assert not (tmp_path / "bars.json").exists()

    async def test_a_refused_result_never_hits_even_with_the_expected_card(self, tmp_path, monkeypatch):
        expected = GOLD[0]["expected_paths"][0]
        item_id = item_id_for(expected)

        class _AmbiguousWithCandidates:
            def __init__(self, *args, **kwargs):
                pass

            async def search(self, user_id, request):
                card = DriveSearchCard(ref=drive_ref.format(EVAL_REF_TAG, item_id), item_id=item_id, name="Scan_0012.pdf",
                                       path=expected, kind="pdf", is_folder=False, summary="Scan_0012.pdf | pdf")
                return DriveSearchResult("ambiguous_folder", cards=(card,))

        monkeypatch.setattr(runner, "DriveSearchService", _AmbiguousWithCandidates)
        report = await _eval(tmp_path, [GOLD[0]])
        row = report["cases"][0]
        assert row["top"] == [[expected]] and row["missing"] == []      # the card is there ...
        assert row["hit"] is False and report["verdict"] == "unrecordable"   # ... but a refusal never hits

    async def test_repeated_tick_errors_stop_the_run(self, monkeypatch):
        class _Broken(FakeEmbeddings):
            async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
                raise RuntimeError("embedding backend exploded")

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
        with pytest.raises(EvalError, match=f"{MAX_ERRORS} ticks ended in error"):
            await index_fixture(FixtureDrive(MANIFEST), InMemoryDriveIndexRepository(), _Broken(), rome,
                                "fake/8", EvalClock(), throttle_pause_s=0, max_ticks=50, **fake_content())
