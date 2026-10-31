"""Drive eval runner: the real tick and the real search over FixtureDrive, offline (FakeEmbeddings, a fixed
place lookup). USER_DRIVE_INDEXING_RFC §17.8; RFC Δ13. Policy over the committed fixture is
tests/unit/services/test_drive_index_hostile_tree.py."""
import json
from datetime import datetime, timezone

import pytest

from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.metrics import OVERALL
from src.domain.connection_scope import ConnectionScope
from scripts.validation.drive_eval.eval_connection import EVAL_CONNECTION_ID
from scripts.validation.drive_eval.runner import (
    EVAL_USER, EXIT_OK, EXIT_REGRESSION, EXIT_SETUP, EvalClock, EvalError, exit_code, index_fixture,
    request_for, run_eval,
)
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import CURRENT_VERSIONS, Reason, Scope

from ..services.drive_index_fakes import FakeEmbeddings
from ..services.test_drive_indexing_service import EXTRACTOR_VERSIONS
from .drive_eval_samples import GOLD, MANIFEST, fake_content, rome


def _write(tmp_path, gold=GOLD, manifest=MANIFEST):
    tree, gold_path = tmp_path / "tree.json", tmp_path / "gold.json"
    tree.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return tree, gold_path, tmp_path / "bars.json", tmp_path / "report.json"


async def _eval(tmp_path, record, gold=GOLD, manifest=MANIFEST):
    tree, gold_path, bars, report = _write(tmp_path, gold, manifest)
    return await run_eval(tree, gold_path, bars, report, embeddings=FakeEmbeddings(), embedding_space="fake/8",
                          place_of=rome, record=record, throttle_pause_s=0, **fake_content())


def _bars(tmp_path):
    return json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))


def test_bare_date_to_covers_the_whole_day():
    req = request_for(GOLD[3])
    assert req.query == "" and req.date_from.isoformat() == "2025-08-01T00:00:00+00:00"
    assert req.date_to.isoformat() == "2025-08-31T23:59:59.999999+00:00"
    assert request_for(GOLD[2]).folder == "Contracts"


async def test_index_settles_with_the_real_tick():
    repo = InMemoryDriveIndexRepository()
    out = await index_fixture(FixtureDrive(MANIFEST), repo, FakeEmbeddings(), rome, "fake/8", EvalClock(),
                              throttle_pause_s=0, **fake_content())
    assert out.statuses.get("error", 0) == 0 and out.ticks >= 1
    assert await repo.due_records(ConnectionScope(EVAL_USER, EVAL_CONNECTION_ID), datetime.max.replace(tzinfo=timezone.utc), 1) == []
    state = await repo.get_sync_state(ConnectionScope(EVAL_USER, EVAL_CONNECTION_ID))
    assert state.continuation_enqueued_at is None
    assert dict(state.applied_versions) == {**CURRENT_VERSIONS, **EXTRACTOR_VERSIONS}

    def rec(path):
        return repo.records.get((EVAL_CONNECTION_ID, item_id_for(path)))

    assert rec("Dumps/site-mirror").reason is Reason.DENSE_TREE
    assert rec("Dumps/site-mirror/page-000.bin") is None
    assert rec("Projects/alek-site").scope is Scope.OPAQUE_ROOT
    assert rec("Projects/alek-site/src/main.py") is None
    assert rec("Photos/Rome 2025/IMG_0001.HEIC").place_name == "Rome (Roma), Italy"
    assert rec("Vault/note-002.md").has_card


async def test_the_runner_keeps_ticking_until_no_continuation_is_pending():
    """A slow clock spends each tick's start budget after a few steps, so onboarding spans many ticks."""
    elapsed = [0.0]

    def slow() -> float:
        elapsed[0] += 100.0
        return elapsed[0]

    repo = InMemoryDriveIndexRepository()
    out = await index_fixture(FixtureDrive(MANIFEST, page_size=50), repo, FakeEmbeddings(), rome, "fake/8",
                              EvalClock(), throttle_pause_s=0, monotonic=slow, **fake_content())
    assert out.ticks > 1 and out.statuses.get("error", 0) == 0
    state = await repo.get_sync_state(ConnectionScope(EVAL_USER, EVAL_CONNECTION_ID))
    assert state.continuation_enqueued_at is None and state.initial_enumeration_done
    assert repo.records.get((EVAL_CONNECTION_ID, item_id_for("Vault/note-002.md"))).has_card


async def test_record_then_compare_passes(tmp_path):
    recorded = await _eval(tmp_path, record=True)
    assert recorded["verdict"] == "recorded" and exit_code(recorded) == EXIT_OK
    bars = _bars(tmp_path)
    assert bars["embedding_space"] == "fake/8" and bars["k"] == 8 and OVERALL in bars["recall"]
    assert set(bars["cases"]) == {c["id"] for c in GOLD} and len(bars["tree_sha256"]) == 64
    hits = {c["id"]: c["hit"] for c in recorded["cases"]}
    assert hits["ex1"] and hits["tr1"]   # exact and transliterated names come through the name stream
    compared = await _eval(tmp_path, record=False)
    assert compared["verdict"] == "pass" and compared["regressions"] == [] and exit_code(compared) == EXIT_OK
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert len(report["cases"]) == len(GOLD) and report["index"]["records"]["carded"] > 0


async def test_falling_below_a_bar_exits_non_zero(tmp_path):
    await _eval(tmp_path, record=True)
    bars = _bars(tmp_path)
    bars["recall"] = {OVERALL: 1.01, "exact": 1.01}      # a bar no run can reach
    bars["cases"]["ex1"] = True
    (tmp_path / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    report = await _eval(tmp_path, record=False)
    assert report["verdict"] == "regression" and exit_code(report) == EXIT_REGRESSION
    assert {r["scope"] for r in report["regressions"]} == {OVERALL, "exact"}


async def test_bars_from_another_k_tree_or_gold_give_no_verdict(tmp_path):
    await _eval(tmp_path, record=True)
    good = _bars(tmp_path)
    (tmp_path / "bars.json").write_text(json.dumps({**good, "k": 10}), encoding="utf-8")
    with pytest.raises(EvalError, match="K changed"):
        await _eval(tmp_path, record=False)
    (tmp_path / "bars.json").write_text(json.dumps(good), encoding="utf-8")
    with pytest.raises(EvalError, match=r"added \['ex2'\], removed \['dt1'\]"):
        await _eval(tmp_path, record=False, gold=GOLD[:3] + [{**GOLD[0], "id": "ex2"}])
    with pytest.raises(EvalError, match="tree.json changed"):
        await _eval(tmp_path, record=False, manifest={**MANIFEST, "items": MANIFEST["items"][:-1]})


@pytest.mark.parametrize("broken, message", [
    ({**GOLD[0], "expected_paths": []}, "no expected_paths"),
    ({**GOLD[0], "case": "vibes"}, "unknown case type"),
    ({**GOLD[0], "expected_paths": ["Nowhere/missing.pdf"]}, "not in the tree"),
    ({**GOLD[0], "k": 9}, "k must be"),
    (GOLD[1], "duplicate id tr1"),
])
async def test_a_malformed_gold_file_gives_no_verdict(tmp_path, broken, message):
    with pytest.raises(EvalError, match=message):
        await _eval(tmp_path, record=True, gold=GOLD + [broken])
    assert not (tmp_path / "bars.json").exists()


async def test_a_case_with_k_1_needs_the_file_first(tmp_path):
    first_only = [{**GOLD[0], "id": "ex1-top", "k": 1}]
    report = await _eval(tmp_path, record=True, gold=GOLD + first_only)
    row = next(c for c in report["cases"] if c["id"] == "ex1-top")
    assert row["k"] == 1 and len(row["top"]) <= 1


async def test_an_empty_index_is_never_recorded(tmp_path, monkeypatch):
    from scripts.validation.drive_eval import runner

    real = runner.index_fixture

    async def no_cards(*args, **kwargs):
        out = await real(*args, **kwargs)
        out.records = {"metadata_only": 0, "carded": 0}
        return out

    monkeypatch.setattr(runner, "index_fixture", no_cards)
    with pytest.raises(EvalError, match="no card"):
        await _eval(tmp_path, record=True)
    assert not (tmp_path / "bars.json").exists()


async def test_a_raising_search_is_a_finding_and_never_a_bar(tmp_path, monkeypatch):
    from scripts.validation.drive_eval import runner

    class _Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, user_id, request):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "DriveSearchService", _Broken)
    report = await _eval(tmp_path, record=True)
    assert report["verdict"] == "search_errors" and report["search_errors"] == len(GOLD)
    assert report["cases"][0]["status"] == "exception: RuntimeError: boom" and not report["cases"][0]["hit"]
    assert not (tmp_path / "bars.json").exists() and exit_code(report) == EXIT_SETUP


async def test_a_refused_case_is_never_recorded(tmp_path, monkeypatch):
    from scripts.validation.drive_eval import runner
    from src.domain.drive_search import DriveSearchResult

    class _Refusing:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, user_id, request):
            return DriveSearchResult("ambiguous_folder")

    monkeypatch.setattr(runner, "DriveSearchService", _Refusing)
    report = await _eval(tmp_path, record=True)
    assert report["verdict"] == "unrecordable" and report["refused"] == len(GOLD)
    assert not (tmp_path / "bars.json").exists() and exit_code(report) == EXIT_SETUP


async def test_no_bars_gives_no_verdict(tmp_path):
    with pytest.raises(EvalError, match="no bars"):
        await _eval(tmp_path, record=False)


async def test_a_new_embedding_space_is_judged_against_the_old_bars(tmp_path):
    """§17.8: an embedding change must not fall below the recorded bars; the report names both spaces."""
    await _eval(tmp_path, record=True)
    bars = {**_bars(tmp_path), "embedding_space": "gemini-embedding-2/768", "recall": {OVERALL: 0.0}}
    (tmp_path / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    report = await _eval(tmp_path, record=False)
    assert report["bars_embedding_space"] == "gemini-embedding-2/768" and report["embedding_space"] == "fake/8"
    assert report["verdict"] == "pass"


def test_cli_records_then_passes_with_the_production_wiring_faked(tmp_path, monkeypatch, capsys):
    """The CLI builds GeminiEmbeddingAdapter, the place table and the real content ports (§24.11); all three are
    swapped here (no network, no model calls)."""
    from scripts.validation.drive_eval import __main__ as cli
    from scripts.validation.drive_eval import content_wiring
    import src.adapters.gemini_embedding_adapter as gemini_mod
    import src.adapters.geo_place_table as geo_mod
    from src.domain.place_lookup import PlaceRow

    class _Adapter(FakeEmbeddings):
        EMBEDDING_SPACE = "fake/8"

        def __init__(self, api_key):
            super().__init__()

    from src.domain.drive_extraction import PARSER, READER
    from ..services.drive_content_fakes import FakeExtractor, FakeTriage

    content_builds = []

    def _content():
        content_builds.append(True)
        return {PARSER: FakeExtractor(), READER: FakeExtractor(READER)}, FakeTriage()

    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)   # never read the developer's .env here
    monkeypatch.setattr(content_wiring, "build_real_content", _content)
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setattr(gemini_mod, "GeminiEmbeddingAdapter", _Adapter)
    monkeypatch.setattr(geo_mod, "load_place_rows", lambda path=None: [PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy")])
    tree, gold, bars, report = _write(tmp_path)
    argv = ["--tree", str(tree), "--gold", str(gold), "--bars", str(bars), "--report", str(report)]
    assert cli.main(argv + ["--record"]) == EXIT_OK
    assert cli.main(argv) == EXIT_OK and "verdict: pass" in capsys.readouterr().out
    assert len(content_builds) == 2   # every run, record or compare, builds the content ports

    from scripts.validation.drive_eval import runner
    from src.domain.drive_search import DriveSearchResult

    class _Refusing:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, user_id, request):
            return DriveSearchResult("ambiguous_folder")

    monkeypatch.setattr(runner, "DriveSearchService", _Refusing)
    assert cli.main(argv) == EXIT_REGRESSION
    out = capsys.readouterr().out
    lost = next(line for line in out.splitlines() if line.startswith("lost since the bars were recorded: "))
    assert "ex1" in lost and "tr1" in lost and "REGRESSION overall" in out
    monkeypatch.delenv("GEMINI_API_KEY")
    assert cli.main(argv) == EXIT_SETUP
