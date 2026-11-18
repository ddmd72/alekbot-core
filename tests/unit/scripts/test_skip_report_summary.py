"""scripts/ci/skip_report_summary.py: the skip report as a Markdown table, and never a failure."""
import json

from scripts.ci.skip_report_summary import main, render

REPORT = {
    "outcomes": {"passed": 5, "skipped": 3, "xfailed": 1},
    "skipped_by_reason": {"needs a | thing": {"count": 2, "tests": ["a", "b"]}, "other": {"count": 1, "tests": ["c"]}},
}


def test_the_summary_lists_outcomes_and_skip_reasons_largest_first():
    text = render(REPORT)
    assert "passed: 5, skipped: 3, xfailed: 1" in text
    assert text.index("| 2 | needs a / thing |") < text.index("| 1 | other |")


def test_a_report_without_skips_has_no_table():
    assert "| Skipped |" not in render({"outcomes": {"passed": 1}, "skipped_by_reason": {}})


def test_a_missing_or_broken_file_is_a_note_not_a_failure(tmp_path, capsys):
    assert main([str(tmp_path / "none.json")]) == 0
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert main([str(broken)]) == 0
    assert capsys.readouterr().out.count("no readable report file") == 2


def test_the_file_round_trips(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(REPORT))
    assert main([str(path)]) == 0
    assert "| 1 | other |" in capsys.readouterr().out


def test_a_report_of_the_wrong_shape_is_a_note_not_a_failure(tmp_path, capsys):
    for body in ('{"skipped_by_reason": {"r": {}}}', '{"skipped_by_reason": {"r": 3}}', "[1, 2]", '{"outcomes": 5}'):
        path = tmp_path / "r.json"
        path.write_text(body)
        assert main([str(path)]) == 0
    assert capsys.readouterr().out.count("not a skip report") == 4
