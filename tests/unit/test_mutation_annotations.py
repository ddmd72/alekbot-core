"""tests/support/mutation_targets --annotations: the verdict of a --summarize run as GitHub workflow commands."""

import json

from tests.support import mutation_targets


def _write(tmp_path, name, module, counts, outcomes, **extra):
    d = tmp_path / "reports"
    d.mkdir(exist_ok=True)
    report = {"module": module, "operators": "default", "tests": ["t.py"], "counts": counts, "outcomes": outcomes, **extra}
    (d / f"{name}.json").write_text(json.dumps(report))


def _outcome(number, status, description="drop filter", lineno=None):
    return {"number": number, "status": status, "lineno": lineno or number, "operator": "where-drop",
            "description": description, "key": f"{number:08x}", "detail": ""}


def _run(tmp_path, expected=None):
    md, combined = mutation_targets.summarize(tmp_path / "reports", expected)
    path = tmp_path / "survivors.json"
    path.write_text(json.dumps(combined))
    return mutation_targets.annotations(json.loads(path.read_text()))


def test_notice_for_clean_partial_and_missing_modules(tmp_path):
    _write(tmp_path, "a", "pkg.clean", {"killed": 2, "survived": 0, "equivalent": 1, "error": 0},
           [_outcome(1, "killed"), _outcome(2, "killed"), _outcome(3, "equivalent")])
    _write(tmp_path, "b", "pkg.part", {"killed": 1, "survived": 0, "equivalent": 0, "error": 0},
           [_outcome(1, "killed")], complete=False, total=5)
    out = _run(tmp_path, ["pkg.clean", "pkg.part", "pkg.gone"])
    assert out == ["::notice title=Mutation summary::pkg.clean: 2/0/1/0, complete"
                   "%0Apkg.gone: missing%0Apkg.part: 1/0/0/0, partial (1 of 5)"]


def test_one_survivor_line(tmp_path):
    _write(tmp_path, "a", "pkg.m", {"killed": 0, "survived": 1, "equivalent": 0, "error": 0},
           [_outcome(1, "survived", "drop filter", lineno=42)])
    out = _run(tmp_path)
    assert len(out) == 2
    assert out[1] == "::warning title=Mutation survivors (1 of 1)::pkg.m L42 where-drop key=00000001 survived: drop filter"


def test_escapes_percent_and_newlines_and_trims_description(tmp_path):
    _write(tmp_path, "a", "pkg.m", {"killed": 0, "survived": 1, "equivalent": 0, "error": 0},
           [_outcome(1, "survived", "100% sure\nsecond\r\nline " + "x" * 400)])
    warning = _run(tmp_path)[1]
    body = warning.split("::", 2)[2]
    assert "\n" not in warning and "\r" not in warning
    assert "100%25 sure" in body
    unescaped = body.split(": ", 1)[1].replace("%25", "%")
    assert len(unescaped) <= 160 and "second line" in unescaped


def test_packing_limit_and_overflow_line(tmp_path):
    _write(tmp_path, "a", "pkg.m", {"killed": 0, "survived": 2000, "equivalent": 0, "error": 0},
           [_outcome(n, "survived", "drop filter " + "y" * 120) for n in range(1, 2001)])
    out = _run(tmp_path)
    warnings = out[1:]
    assert 1 < len(warnings) <= 9
    for w in warnings:
        assert len(w.split("::", 2)[2]) <= 6000
    assert warnings[0].startswith(f"::warning title=Mutation survivors (1 of {len(warnings)})::")
    assert warnings[-1].split("%0A")[-1].startswith("… ") and "more: see the survivors artifact" in warnings[-1]


def test_no_findings_only_the_notice(tmp_path):
    _write(tmp_path, "a", "pkg.m", {"killed": 1, "survived": 0, "equivalent": 0, "error": 0}, [_outcome(1, "killed")])
    out = _run(tmp_path)
    assert len(out) == 1 and out[0].startswith("::notice title=Mutation summary::")


def test_cli_prints_the_lines(tmp_path, capsys):
    _write(tmp_path, "a", "pkg.m", {"killed": 0, "survived": 1, "equivalent": 0, "error": 0}, [_outcome(1, "survived")])
    _, combined = mutation_targets.summarize(tmp_path / "reports")
    path = tmp_path / "s.json"
    path.write_text(json.dumps(combined))
    assert mutation_targets.main(["--annotations", str(path)]) == 0
    assert capsys.readouterr().out.count("\n") == 2
