"""tests/support/mutation_targets: the registry is complete and on disk; the matrix and the summary have their shape."""

import json
from collections import Counter
from pathlib import Path

from tests.support import mutation_targets
from tests.support.mutation_targets import TARGETS, matrix, summarize

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_registered_path_exists():
    for t in TARGETS:
        assert t.tests, t.module
        for path in t.tests:
            assert (REPO_ROOT / path).is_file(), (t.module, path)
        if t.equivalent:
            assert (REPO_ROOT / t.equivalent).is_file(), (t.module, t.equivalent)


def test_every_registered_module_resolves_to_a_source_file():
    for t in TARGETS:
        rel = Path(*t.module.split("."))
        assert (REPO_ROOT / rel).with_suffix(".py").is_file(), t.module


def test_modules_and_slugs_are_unique():
    assert len({t.module for t in TARGETS}) == len(TARGETS)
    assert len({t.slug for t in TARGETS}) == len(TARGETS)
    assert all("." not in t.slug for t in TARGETS)


def test_every_equivalent_file_belongs_to_a_registered_target():
    on_disk = {p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "tests/unit").rglob("*_equivalent_mutants.txt")}
    registered = {t.equivalent for t in TARGETS if t.equivalent}
    assert on_disk <= registered, f"unregistered equivalent files: {sorted(on_disk - registered)}"
    assert registered <= on_disk
    # one file per package: only the wiring file is shared (by the six composition modules)
    shared = {path for path, n in Counter(t.equivalent for t in TARGETS).items() if n > 1}
    assert shared == {"tests/unit/composition/wiring_equivalent_mutants.txt"}


def test_matrix_shape():
    data = matrix()
    assert list(data) == ["include"] and len(data["include"]) == len(TARGETS)
    for row in data["include"]:
        assert set(row) == {"module", "slug", "tests", "equivalent", "operators"}
        assert all(isinstance(v, str) for v in row.values())
    first = data["include"][0]
    assert first["tests"].split() == list(TARGETS[0].tests)


def test_matrix_can_be_restricted_to_one_module():
    data = matrix("src.services.fact_answer_service")
    assert [r["module"] for r in data["include"]] == ["src.services.fact_answer_service"]
    assert matrix("no.such.module")["include"] == []


def test_matrix_cli_prints_one_json_line(capsys):
    assert mutation_targets.main(["--matrix"]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    assert json.loads(out) == matrix()


def test_matrix_cli_refuses_an_unknown_module(capsys):
    assert mutation_targets.main(["--matrix", "--module", "no.such.module"]) == 2


def _report(module, counts, outcomes):
    return {"module": module, "operators": "default", "tests": ["t.py"], "counts": counts, "outcomes": outcomes}


def _outcome(number, status, lineno, key, detail=""):
    return {"number": number, "status": status, "lineno": lineno, "operator": "where-drop",
            "description": f"drop filter {number}", "key": key, "detail": detail}


def test_summary_lists_survivors_and_errors_and_marks_clean_modules(tmp_path):
    (tmp_path / "mutation-a").mkdir()
    (tmp_path / "mutation-b").mkdir()
    (tmp_path / "mutation-a" / "a.json").write_text(json.dumps(_report(
        "pkg.clean", {"killed": 2, "survived": 0, "equivalent": 1, "error": 0},
        [_outcome(1, "killed", 3, "aaaaaaaa"), _outcome(2, "killed", 4, "bbbbbbbb"), _outcome(3, "equivalent", 5, "cccccccc")])))
    (tmp_path / "mutation-b" / "b.json").write_text(json.dumps(_report(
        "pkg.dirty", {"killed": 1, "survived": 1, "equivalent": 0, "error": 1},
        [_outcome(1, "killed", 7, "dddddddd"), _outcome(2, "survived", 9, "eeeeeeee"),
         _outcome(3, "error", 11, "ffffffff", "never loaded")])))

    md, combined = summarize(tmp_path)

    assert "| pkg.clean | 2 | 0 | 1 | 0 |" in md
    assert "| pkg.dirty | 1 | 1 | 0 | 1 |" in md
    assert "pkg.clean: clean" in md
    assert "## pkg.dirty" in md
    assert "[survived] L9 where-drop: drop filter 2 (key eeeeeeee)" in md
    assert "[error] L11 where-drop: drop filter 3 (key ffffffff) - never loaded" in md
    assert "dddddddd" not in md and "## pkg.clean" not in md
    assert [(f["module"], f["status"], f["key"]) for f in combined["findings"]] == [
        ("pkg.dirty", "survived", "eeeeeeee"), ("pkg.dirty", "error", "ffffffff")]
    assert [m["module"] for m in combined["modules"]] == ["pkg.clean", "pkg.dirty"]


def test_summarize_cli_writes_both_files(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "x.json").write_text(json.dumps(_report("pkg.x", {"killed": 1, "survived": 0, "equivalent": 0, "error": 0},
                                                       [_outcome(1, "killed", 1, "11111111")])))
    md_path, json_path = tmp_path / "s.md", tmp_path / "s.json"
    assert mutation_targets.main(["--summarize", str(reports), "--out-md", str(md_path), "--out-json", str(json_path)]) == 0
    assert "pkg.x: clean" in md_path.read_text()
    assert json.loads(json_path.read_text())["findings"] == []
