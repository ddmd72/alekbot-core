"""The passage-recall eval (scripts/validation/drive_eval/excerpts_eval.py, plan S1.3): offline, on a tiny in-memory
fixture with LexicalEmbeddings. A case hits when its substring is in the expected path's returned text and misses
otherwise; bars that cannot judge refuse before anything is built; the cache probe calls nothing."""
import io
import json
import zipfile

import pytest

from scripts.validation.drive_eval import excerpts_eval
from scripts.validation.drive_eval.excerpts_eval import bars_mismatch, evaluate, exit_code, judge, probe
from scripts.validation.drive_eval.lexical_embeddings import EMBEDDING_SPACE, LexicalEmbeddings
from scripts.validation.drive_eval.runner import EvalError, gold_sha256, sha256_of

from src.domain.drive_extraction import PARSER, READER, ExtractionOutcome, ExtractionStatus

from ..services.drive_content_fakes import FakeExtractor, FakeTriage
from .drive_eval_samples import rome

PLAN = "R&D/Tom's plan.docx"
BUDGET = "R&D/Acme budget.docx"
PLAN_TEXT = "Tom Baker owns the launch plan. The pilot with Acme starts in March and runs for nine weeks."
BUDGET_TEXT = "Acme budget: the hardware line is 4,200 euros, approved by Tom Baker."


def _case(cid, query, path, substring):
    return {"id": cid, "case": "clause", "query": query, "lang": "en", "goal": "G2", "filters": {},
            "expected_paths": [path], "expected_substring": substring}


GOLD = [_case("plan-weeks", "how long does the Acme pilot run", PLAN, "nine weeks"),
        _case("budget-hardware", "hardware budget line", BUDGET, "4,200 euros"),
        _case("plan-wrong-text", "who owns the launch plan", PLAN, "text no file holds")]


def _docx() -> bytes:
    """Bytes the sniffer proves to be a docx; the scripted parser supplies the text."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _bars(tmp_path, tree, gold, window_cases, overrides=None):
    must = sorted(c for c, hit in window_cases.items() if hit)
    return {"embedding_space": EMBEDDING_SPACE, "tree_sha256": sha256_of(tree), "gold_sha256": gold_sha256([gold]),
            "window_bars_recorded_at": "t0", "recall_min": 0.6, "must_hit": must, **(overrides or {})}


def _files(tmp_path, bars_overrides=None, window_cases=None, gold=None):
    (tmp_path / "plan.docx").write_bytes(_docx())
    (tmp_path / "budget.docx").write_bytes(_docx())
    tree, gold_file, bars, window = (tmp_path / n for n in ("tree.json", "gold.json", "bars_excerpts.json", "bars.json"))
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    tree.write_text(json.dumps({"items": [
        {"path": PLAN, "folder": False, "mime": mime, "content": "plan.docx"},
        {"path": BUDGET, "folder": False, "mime": mime, "content": "budget.docx"}]}), encoding="utf-8")
    gold_file.write_text(json.dumps(gold or GOLD), encoding="utf-8")
    window_cases = window_cases or {"plan-weeks": True, "budget-hardware": True, "plan-wrong-text": False}
    window.write_text(json.dumps({"recorded_at": "t0", "cases": window_cases}), encoding="utf-8")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    bars.write_text(json.dumps(_bars(tmp_path, tree, gold_file, window_cases, bars_overrides)), encoding="utf-8")
    parser = FakeExtractor(PARSER, outcomes={
        "Tom's plan.docx": ExtractionOutcome(ExtractionStatus.TEXT, PLAN_TEXT),
        "Acme budget.docx": ExtractionOutcome(ExtractionStatus.TEXT, BUDGET_TEXT)})
    content = {"extractors": {PARSER: parser, READER: FakeExtractor(READER)}, "triage": FakeTriage()}
    return dict(tree=tree, gold_paths=[gold_file], bars_path=bars, window_bars_path=window, snapshot=snapshot,
                embeddings=LexicalEmbeddings(), embedding_space=EMBEDDING_SPACE, place_of=rome,
                build_content=lambda: (content["extractors"], content["triage"]), cache_dir=tmp_path / "cache")


async def test_a_hit_has_the_substring_in_the_expected_files_text_and_a_miss_does_not(tmp_path):
    report = await evaluate(**_files(tmp_path))
    rows = {r["id"]: r for r in report["rows"]}
    assert rows["plan-weeks"]["hit"] and rows["plan-weeks"]["path_hit"] and rows["plan-weeks"]["substring_hit"]
    assert rows["budget-hardware"]["hit"]
    wrong = rows["plan-wrong-text"]
    assert wrong["path_hit"] and not wrong["substring_hit"] and not wrong["hit"]
    assert report["hits"] == 2 and report["cases"] == 3 and report["recall"] == pytest.approx(2 / 3, abs=1e-5)
    assert report["lost_cases"] == [] and report["verdict"] == "pass" and exit_code(report) == 0


async def test_the_substring_in_another_files_text_is_not_a_hit(tmp_path):
    swapped = [_case("swapped", "hardware budget line", PLAN, "4,200 euros")]
    report = await evaluate(**_files(tmp_path, gold=swapped, window_cases={"swapped": False},
                                     bars_overrides={"recall_min": 0.0}))
    assert not report["rows"][0]["hit"]


async def test_a_must_hit_case_that_misses_fails_even_above_the_recall_floor(tmp_path):
    kwargs = _files(tmp_path, window_cases={"plan-weeks": True, "budget-hardware": True, "plan-wrong-text": True},
                    bars_overrides={"recall_min": 0.5})
    report = await evaluate(**kwargs)
    assert report["recall"] >= 0.5
    assert report["lost_cases"] == ["plan-wrong-text"] and report["verdict"] == "regression"
    assert exit_code(report) == 1


async def test_recall_below_the_floor_fails(tmp_path):
    report = await evaluate(**_files(tmp_path, bars_overrides={"recall_min": 0.9}))
    assert report["lost_cases"] == [] and report["verdict"] == "regression"


@pytest.mark.parametrize("override, message", [
    ({"tree_sha256": "0" * 64}, "tree.json changed"),
    ({"gold_sha256": "0" * 64}, "gold file changed"),
    ({"window_bars_recorded_at": "t-old"}, "bars.json was re-recorded"),
    ({"must_hit": ["plan-weeks"]}, "must_hit differs"),
    ({"must_hit": ["plan-weeks", "budget-hardware", "ghost"]}, "not in the gold"),
])
async def test_bars_that_cannot_judge_refuse_before_anything_is_built(tmp_path, override, message):
    kwargs = _files(tmp_path, bars_overrides=override)

    def never():
        raise AssertionError("built")
    kwargs["build_content"] = never
    with pytest.raises(EvalError, match=message):
        await evaluate(**kwargs)


async def test_bars_for_another_embedding_space_refuse(tmp_path):
    with pytest.raises(EvalError, match="the bars are for other/1"):
        await evaluate(**_files(tmp_path, bars_overrides={"embedding_space": "other/1"}))


async def test_the_second_run_loads_the_cached_index_and_builds_nothing(tmp_path):
    kwargs = _files(tmp_path)
    first = await evaluate(**kwargs)

    def never():
        raise AssertionError("a cache hit must not build")
    kwargs["build_content"] = never
    second = await evaluate(**kwargs)
    assert first["index_cache"] == "miss" and second["index_cache"] == "hit"
    assert [r["hit"] for r in second["rows"]] == [r["hit"] for r in first["rows"]]


async def test_probe_says_miss_then_hit_and_calls_no_model(tmp_path):
    kwargs = _files(tmp_path)
    args = (kwargs["tree"], kwargs["snapshot"], EMBEDDING_SPACE, kwargs["cache_dir"])
    assert probe(*args)["cached"] is False
    await evaluate(**kwargs)
    assert probe(*args)["cached"] is True


def test_judge_names_search_errors():
    rows = [{"id": "a", "hit": False, "error": True}]
    assert judge(rows, {"recall_min": 0.0, "must_hit": []})["verdict"] == "search_errors"


def test_the_shipped_bars_agree_with_the_shipped_fixtures():
    fixtures = excerpts_eval._FIXTURES
    bars = json.loads((fixtures / "bars_excerpts.json").read_text(encoding="utf-8"))
    window = json.loads((fixtures / "bars.json").read_text(encoding="utf-8"))
    gold = json.loads((fixtures / "gold_g2.json").read_text(encoding="utf-8"))
    assert bars_mismatch(bars, tree_sha256=sha256_of(fixtures / "tree.json"),
                         gold_sha=gold_sha256([fixtures / "gold_g2.json"]), case_ids=[c["id"] for c in gold],
                         window_bars=window) == []
    assert bars["recall_min"] == window["recall"]["G2"] == 0.8125
