"""Per-case rules of the drive eval (USER_DRIVE_INDEXING_RFC §17.8):
- `min_hits`: a case with many right answers (every photo of a trip) hits when that many are in its top K;
- any case lost since the bars were recorded fails the run, not only a fall in a scope's recall;
- a `known_hard` case is run and reported, but is neither in the recall bars nor a lost case;
- `semantic` is a case type (meaning only, out of the name stream's reach)."""
import json

import pytest

from scripts.validation.drive_eval.metrics import CASE_TYPES, case_hit
from scripts.validation.drive_eval.runner import EXIT_REGRESSION, EvalError, exit_code, run_eval

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import GOLD, MANIFEST, fake_content, rome


def test_min_hits_lowers_the_bar_below_min_of_expected_and_k():
    many = [f"p{i}" for i in range(12)]
    assert not case_hit(many, set(many[:5]), 8)
    assert case_hit(many, set(many[:5]), 8, min_hits=5)
    assert not case_hit(many, set(many[:4]), 8, min_hits=5)


def test_semantic_is_a_case_type():
    assert "semantic" in CASE_TYPES


async def _eval(tmp_path, record, gold=GOLD):
    tree, gold_path = tmp_path / "tree.json", tmp_path / "gold.json"
    tree.write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return await run_eval(tree, gold_path, tmp_path / "bars.json", tmp_path / "report.json",
                          embeddings=FakeEmbeddings(), embedding_space="fake/8", place_of=rome, record=record,
                          throttle_pause_s=0, **fake_content())


@pytest.mark.parametrize("min_hits", [0, 2, "1", True])
async def test_min_hits_must_fit_the_case(tmp_path, min_hits):
    with pytest.raises(EvalError, match="min_hits"):
        await _eval(tmp_path, True, GOLD + [{**GOLD[0], "id": "bad", "min_hits": min_hits}])


async def test_known_hard_must_say_why(tmp_path):
    with pytest.raises(EvalError, match="known_hard"):
        await _eval(tmp_path, True, GOLD + [{**GOLD[0], "id": "bad", "known_hard": ""}])


async def test_a_lost_case_fails_the_run_even_when_no_scope_recall_falls(tmp_path, monkeypatch):
    from scripts.validation.drive_eval import runner

    recorded = await _eval(tmp_path, record=True)
    assert next(c for c in recorded["cases"] if c["id"] == "ex1")["hit"]
    bars = json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))
    bars["recall"] = {scope: 0.0 for scope in bars["recall"]}   # no scope can fall below its bar
    (tmp_path / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    real = runner.run_cases

    async def lose_ex1(*args, **kwargs):
        run = await real(*args, **kwargs)
        run.outcomes = [type(o)(o.case_id, o.case, False) if o.case_id == "ex1" else o for o in run.outcomes]
        return run

    monkeypatch.setattr(runner, "run_cases", lose_ex1)
    report = await _eval(tmp_path, record=False)
    assert report["regressions"] == [] and report["lost_cases"] == ["ex1"]
    assert report["verdict"] == "regression" and exit_code(report) == EXIT_REGRESSION


async def test_a_known_hard_case_is_reported_outside_the_bars(tmp_path):
    hard = {**GOLD[0], "id": "hard1", "query": "photos", "known_hard": "a type word in drive_query"}
    report = await _eval(tmp_path, record=True, gold=GOLD + [hard])
    bars = json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))
    assert "hard1" not in bars["cases"] and report["recall"]["overall"]["cases"] == len(GOLD)
    assert set(report["known_hard"]) == {"hard1"}
    row = next(c for c in report["cases"] if c["id"] == "hard1")
    assert row["known_hard"] == "a type word in drive_query"
