"""scripts/quality/friction.py: the weekly friction numbers over hand-built units (no git here)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.quality import friction
from scripts.quality.friction import report_weeks, to_markdown, weekly_friction
from scripts.quality.git_units import BranchCommit, FileChange, GitParseError, Unit

MON = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday
UNTIL = date(2026, 1, 12)  # the next Monday: exactly one complete week, labelled 2026-01-05
SRC = "src/services/alpha.py"
TEST_A = "tests/unit/services/test_alpha.py"
TEST_B = "tests/unit/services/test_beta.py"
LEDGER = "tests/unit/alpha_allowlist.txt"


def unit(n: int, *files: FileChange, at: datetime = MON + timedelta(days=1), pr: int | None = None,
         branch: tuple[BranchCommit, ...] = ()) -> Unit:
    return Unit(sha=f"{n:040x}", date=at, title=f"feat(drive): change {n}", pr=pr, files=files,
                branch_commits=branch)


def f(path: str, status: str = "M", added: int = 0, deleted: int = 0, defs_added: int = 0,
      defs_removed: int = 0) -> FileChange:
    return FileChange(path, status, added, deleted, test_defs_added=defs_added, test_defs_removed=defs_removed)


def one_week(*units: Unit) -> dict:
    (row,) = weekly_friction(list(units), weeks=1, until=UNTIL)["weeks"]
    return row


def test_weeks_are_complete_mondays_and_monday_midnight_starts_a_new_week():
    weeks = report_weeks(2, date(2026, 1, 18))  # a Sunday: the week of 2026-01-12 has not ended
    assert [w.label for w in weeks] == ["2025-12-29", "2026-01-05"]
    assert [w.label for w in report_weeks(1, date(2026, 1, 19))] == ["2026-01-12"]  # a Monday: the week ended that day
    on_the_edge = unit(1, f(SRC, added=10), at=MON + timedelta(days=7))  # Monday 00:00:00 UTC
    just_before = unit(2, f(SRC, added=10), at=MON + timedelta(days=7) - timedelta(seconds=1))
    rows = weekly_friction([on_the_edge, just_before], weeks=2, until=date(2026, 1, 19))["weeks"]
    assert [(r["week"], r["units"]) for r in rows] == [("2026-01-05", 1), ("2026-01-12", 1)]


def test_an_incomplete_week_is_not_reported():
    report = weekly_friction([unit(1, f(SRC, added=10), at=MON + timedelta(days=8))], weeks=1, until=UNTIL)
    assert [r["week"] for r in report["weeks"]] == ["2026-01-05"]
    assert report["weeks"][0]["units"] == 0


def test_drag_counts_existing_test_lines_of_src_units_only():
    row = one_week(
        unit(1, f(SRC, added=80, deleted=20), f(TEST_A, added=30, deleted=10), f(TEST_B, "A", added=500)),
        unit(2, f(SRC, "R", added=0, deleted=0), f(TEST_A, "R", added=5, deleted=5)),
        unit(3, f(TEST_A, added=1000)),  # changes no src: outside drag
    )
    assert (row["src_lines_changed"], row["existing_test_lines_changed"]) == (100, 50)
    assert row["drag"] == 0.5
    assert (row["units"], row["units_changing_src"]) == (3, 2)


def test_drag_and_bookkeeping_are_null_without_src_lines():
    row = one_week(unit(1, f(TEST_A, added=10)))
    assert row["drag"] is None and row["bookkeeping_per_kloc"] is None and row["src_lines_changed"] == 0


def test_bookkeeping_counts_existing_ledgers_and_mechanical_branch_commits():
    mechanical = BranchCommit("m" * 40, "test(mechanical): rename a fixture",
                              (f(TEST_A, added=3, deleted=3), f("docs/alpha.md", added=50), f(SRC, added=9)))
    other = BranchCommit("o" * 40, "feat(drive): add a field", (f(TEST_A, added=40),))
    not_prefix = BranchCommit("p" * 40, "fix: test(mechanical) in the middle", (f(TEST_A, added=40),))
    row = one_week(
        unit(1, f(SRC, added=200), f(LEDGER, added=2), f("tests/unit/new_list.txt", "A", added=70),
             f("tests/fixtures/alpha.txt", added=90), f(TEST_A, added=43, deleted=3),  # TEST_A existed: M
             branch=(mechanical, other, not_prefix)),
        unit(2, f(LEDGER, added=500)),  # no src change: outside bookkeeping
    )
    assert (row["existing_ledger_lines_changed"], row["mechanical_test_lines_changed"]) == (2, 6)
    assert row["bookkeeping_per_kloc"] == pytest.approx(40.0)  # (2 + 6) * 1000 / 200


def test_radius_median_p90_max_and_top_order():
    files_per_unit = [0, 1, 1, 2, 2, 3, 3, 4, 5, 9]
    units = [
        unit(i, f(SRC, added=1), *(f(f"tests/unit/test_{k}.py") for k in range(n)),
             f(TEST_B, "A", added=1), at=MON + timedelta(hours=i), pr=100 + i)
        for i, n in enumerate(files_per_unit)
    ]
    radius = one_week(*units)["radius"]
    assert (radius["median"], radius["p90"], radius["max"]) == (2.5, 5, 9)
    assert [(t["pr"], t["files"]) for t in radius["top"]] == [(109, 9), (108, 5), (107, 4)]
    assert radius["top"][0]["title"] == "feat(drive): change 9" and radius["top"][0]["sha"] == f"{9:040x}"


def test_radius_ties_keep_the_older_unit_first_and_empty_week_is_null():
    a = unit(1, f(SRC, added=1), f(TEST_A), at=MON + timedelta(hours=5))
    b = unit(2, f(SRC, added=1), f(TEST_B), at=MON + timedelta(hours=1))
    assert [t["sha"] for t in one_week(a, b)["radius"]["top"]] == [b.sha, a.sha]
    assert one_week()["radius"] == {"median": None, "p90": None, "max": None, "top": []}


def test_test_counts_over_all_units_and_new_file_share():
    row = one_week(
        unit(1, f(SRC, added=1), f(TEST_A, added=10, defs_added=1, defs_removed=2)),
        unit(2, f(TEST_B, "A", added=40, defs_added=3)),  # no src change: still counted
        unit(3, f("tests/unit/test_gone.py", "D", deleted=8, defs_removed=1)),
        unit(4, f("src/skills/alpha/notes.txt", "A", added=5, defs_added=7)),  # not a test file
    )
    assert (row["tests_added"], row["tests_removed"], row["tests_added_in_new_files"]) == (4, 3, 3)
    assert (row["new_test_files"], row["removed_test_files"]) == (1, 1)
    assert row["new_file_share"] == 0.75


def test_new_file_share_is_null_when_no_test_was_added():
    assert one_week(unit(1, f(TEST_A, deleted=4, defs_removed=1)))["new_file_share"] is None


def test_hot_test_files_over_the_last_four_reported_weeks():
    until = date(2026, 2, 9)  # five complete weeks: 2026-01-05 .. 2026-02-02
    week = lambda k: MON + timedelta(days=7 * k + 1)  # noqa: E731
    units = [
        unit(1, f(TEST_A), f(TEST_B), at=week(0)),  # the first of five weeks: outside the four
        unit(2, f(TEST_A), at=week(0)),
        unit(3, f(TEST_B), f("tests/unit/test_c.py"), at=week(1)),
        unit(4, f(TEST_B), f("tests/unit/test_d.py", "A"), at=week(2)),
        unit(5, f("tests/unit/test_c.py"), f(TEST_A), at=week(4)),
        unit(6, *(f(f"tests/unit/test_{k}.py") for k in "efgh"), at=week(3)),
    ]
    hot = weekly_friction(units, weeks=5, until=until)["hot_test_files"]
    assert hot == [
        {"path": TEST_B, "units": 2},
        {"path": "tests/unit/test_c.py", "units": 2},
        {"path": TEST_A, "units": 1},
        {"path": "tests/unit/test_e.py", "units": 1},
        {"path": "tests/unit/test_f.py", "units": 1},
    ]


def test_markdown_is_a_header_and_one_line_per_week():
    report = weekly_friction([unit(1, f(SRC, added=3), f(TEST_A, added=1, defs_added=1))], weeks=2,
                             until=UNTIL)
    lines = to_markdown(report).splitlines()
    assert lines[0].startswith("# week | units |")
    header = [c.strip("# ") for c in lines[0].split(" | ")]
    rows = [dict(zip(header, line.split(" | "))) for line in lines[1:]]
    assert [r["week"] for r in rows] == ["2025-12-29", "2026-01-05"]
    assert rows[0]["drag"] == "-" and rows[0]["units"] == "0"
    assert rows[1]["drag"] == "0.33" and rows[1]["radius_median"] == "1" and rows[1]["new_file_share"] == "0.00"


def test_mechanical_lines_count_only_test_files_the_unit_modifies():
    mechanical = BranchCommit("m" * 40, "test(mechanical): follow a rename", (
        f(TEST_A, added=2, deleted=2),  # existing in the unit: counts
        f(TEST_B, "A", added=10),  # created by the pull request: does not count
        f("tests/unit/test_c.py", added=5),  # absent from the unit's net diff: does not count
        f("tests/unit/test_gone.py", deleted=3),  # deleted by the pull request: does not count
    ))
    row = one_week(unit(1, f(SRC, added=100), f(TEST_A, added=2, deleted=2), f(TEST_B, "A", added=10),
                        f("tests/unit/test_gone.py", "D", deleted=3), branch=(mechanical,)))
    assert row["mechanical_test_lines_changed"] == 4
    assert row["bookkeeping_per_kloc"] == pytest.approx(40.0)


def test_unreadable_git_text_exits_2_with_one_line(monkeypatch, capsys):
    def unreadable(*args, **kwargs):
        raise GitParseError("raw and numstat entries do not match")

    monkeypatch.setattr(friction, "read_units", unreadable)
    assert friction.main(["--until", "2026-01-12", "--weeks", "1"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and len(out.err.strip().splitlines()) == 1 and "numstat" in out.err
