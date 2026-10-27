"""tests/support/mutation_targets: the registry is complete and on disk; the matrix and the summary have their shape."""

import json
from collections import Counter
from pathlib import Path

from tests.support import mutation_targets
from tests.support.mutation_targets import TARGETS, matrix, summarize
from tests.support.mutation_targets import tests_for as computed_tests_for

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_registered_path_exists_and_every_module_has_tests():
    for t in TARGETS:
        assert t.tests, t.module
        for path in (*t.tests, *t.extra_tests):
            assert (REPO_ROOT / path).is_file(), (t.module, path)
        if t.equivalent:
            assert (REPO_ROOT / t.equivalent).is_file(), (t.module, t.equivalent)
        assert computed_tests_for(t.module), t.module


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
    assert first["tests"].split() == list(TARGETS[0].tests) == sorted(set(first["tests"].split()))


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


def test_firestore_repo_tests_include_the_kits_reached_through_tests_support():
    found = set(computed_tests_for("src.adapters.firestore_repo"))
    assert {
        "tests/unit/adapters/test_fact_store_roundtrip.py",
        "tests/unit/adapters/test_fact_repository_kit.py",
        "tests/unit/adapters/test_fact_repository_versions_kit.py",
        "tests/unit/adapters/test_fact_management_kit.py",
        "tests/unit/adapters/test_fact_write_kit.py",
    } <= found


def test_application_graph_tests_include_every_prod_graph_user():
    found = set(computed_tests_for("src.composition.application_graph"))
    users = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "tests/unit").rglob("test_*.py")
        if p != Path(__file__) and "tests.support.prod_graph" in p.read_text(encoding="utf-8")
    }
    assert users, "the grep found no prod_graph user"
    assert users <= found
    # three of the five P9 files reach the builder only through main (never followed into src/): they ride in extra_tests
    target = next(t for t in TARGETS if t.module == "src.composition.application_graph")
    assert {
        "tests/unit/test_main_boots_offline.py",
        "tests/unit/test_worker_triggers.py",
        "tests/unit/composition/test_application_graph_wiring.py",
        "tests/unit/composition/test_application_graph_values.py",
        "tests/unit/test_main_wiring_values.py",
    } <= set(target.tests)


def test_main_tests_include_the_boot_test():
    assert "tests/unit/test_main_boots_offline.py" in computed_tests_for("main")


def test_tests_for_follows_imports_through_tests_only(tmp_path):
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/target.py": "X = 1\n",
        "src/pkg/other.py": "from src.pkg import target\n",
        "tests/__init__.py": "",
        "tests/support/__init__.py": "",
        "tests/support/helper.py": "from src.pkg.target import X\n",
        "tests/support/outer.py": "from tests.support import helper\n",
        "tests/unit/__init__.py": "",
        "tests/unit/test_via_support.py": "from tests.support import outer\n",
        "tests/unit/test_direct.py": "from src.pkg import target\n",
        "tests/unit/test_relative.py": "from ..support import helper\n",
        "tests/unit/test_via_src.py": "from src.pkg.other import X\n",
        "tests/unit/test_unrelated.py": "import os\n",
        "tests/unit/conftest.py": "from src.pkg import target\n",
        "tests/unit/test_via_conftest.py": "from tests.unit import conftest\n",
        "tests/unit/sub/__init__.py": "",
        "tests/unit/sub/test_rel.py": "from ...support import helper\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert computed_tests_for("src.pkg.target", tmp_path) == (
        "tests/unit/sub/test_rel.py",
        "tests/unit/test_direct.py",
        "tests/unit/test_relative.py",
        "tests/unit/test_via_support.py",
    )


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
