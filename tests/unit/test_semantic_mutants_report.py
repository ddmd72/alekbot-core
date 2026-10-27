"""The driver's --report and --no-fail, in process: the child pytest runs are replaced by a fake with known verdicts."""

import json

import pytest

from tests.support.semantic_mutants import driver
from tests.support.semantic_mutants.operators import enumerate_mutants

TOY = "def pair(x):\n    return x.set(1, {'a': 1, 'b': 2})\n"


@pytest.fixture
def toy(tmp_path, monkeypatch):
    (tmp_path / "toy_report_mod.py").write_text(TOY)
    mutants = enumerate_mutants(TOY)
    assert len(mutants) >= 2
    verdicts = {mutants[0].number: "survived", mutants[1].number: "error"}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(driver, "run_pytest", lambda *a, **k: Done())
    monkeypatch.setattr(
        driver, "run_mutant",
        lambda m, *a, **k: driver.Outcome(m, verdicts.get(m.number, "killed"), "boom" if verdicts.get(m.number) == "error" else "")
    )
    return tmp_path, mutants


def run(tmp_path, *extra):
    return driver.main(["--module", "toy_report_mod", "--tests", "t.py", "--cwd", str(tmp_path), *extra])


def test_report_carries_counts_and_one_entry_per_mutant(toy):
    tmp_path, mutants = toy
    report = tmp_path / "out" / "r.json"  # the parent directory is created

    run(tmp_path, "--report", str(report), "--no-fail")

    data = json.loads(report.read_text())
    assert data["module"] == "toy_report_mod" and data["operators"] == "default" and data["tests"] == ["t.py"]
    assert data["counts"] == {"killed": len(mutants) - 2, "survived": 1, "equivalent": 0, "error": 1}
    assert [o["number"] for o in data["outcomes"]] == [m.number for m in mutants]
    first = data["outcomes"][0]
    assert first["status"] == "survived"
    assert (first["lineno"], first["operator"], first["description"], first["key"]) == (
        mutants[0].lineno, mutants[0].operator, mutants[0].description, mutants[0].key)
    assert data["outcomes"][1]["detail"] == "boom"


def test_the_report_is_rewritten_after_every_mutant_and_complete_only_at_the_end(toy, monkeypatch):
    tmp_path, mutants = toy
    report = tmp_path / "r.json"
    seen = []
    real = driver.run_mutant

    def spying(m, *a, **k):
        if report.exists():
            seen.append(json.loads(report.read_text()))
        return real(m, *a, **k)

    monkeypatch.setattr(driver, "run_mutant", spying)
    run(tmp_path, "--report", str(report), "--no-fail")

    assert len(seen) == len(mutants) - 1  # absent before the first mutant, present before each later one
    assert [len(d["outcomes"]) for d in seen] == list(range(1, len(mutants)))
    assert all(d["complete"] is False and d["total"] == len(mutants) for d in seen)
    final = json.loads(report.read_text())
    assert final["complete"] is True and len(final["outcomes"]) == len(mutants)


def test_equivalent_mutants_are_reported_as_such(toy):
    tmp_path, mutants = toy
    eq = tmp_path / "eq.txt"
    eq.write_text(f"{mutants[0].key}  # unobservable\n")
    report = tmp_path / "r.json"

    run(tmp_path, "--report", str(report), "--equivalent", str(eq), "--no-fail")

    first = json.loads(report.read_text())["outcomes"][0]
    assert (first["status"], first["detail"]) == ("equivalent", "unobservable")


def test_exit_codes_are_unchanged_without_no_fail(toy):
    tmp_path, _ = toy
    assert run(tmp_path) == 1


def test_no_fail_exits_zero_on_survivors_and_errors(toy):
    tmp_path, _ = toy
    assert run(tmp_path, "--no-fail") == 0


def test_no_report_is_written_unless_asked(toy):
    tmp_path, _ = toy
    run(tmp_path, "--no-fail")
    assert not list(tmp_path.rglob("*.json"))


def test_a_failing_baseline_still_exits_two_with_no_fail(toy, monkeypatch):
    tmp_path, _ = toy

    class Failed:
        returncode = 1
        stdout = "boom"
        stderr = ""

    monkeypatch.setattr(driver, "run_pytest", lambda *a, **k: Failed())
    assert run(tmp_path, "--no-fail") == 2
