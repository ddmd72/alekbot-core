"""Weekly test friction from git history: how much existing test code a change to `src` drags along.

Usage: python -m scripts.quality.friction [--ref origin/main] [--weeks 8] [--until YYYY-MM-DD] [--format json|md]

Units are the first-parent commits of the ref (git_units.py). A week runs Monday 00:00 UTC to the next Monday, is
labelled by its Monday, and only complete weeks are reported: the last one is the latest week that ended on or before
`--until` (default today, UTC). Per week:
- `drag`: lines changed in existing (M/R) test files / lines changed in src, over the units that change src;
- `bookkeeping_per_kloc`: (lines changed in existing ledger files + lines of `test(mechanical)` branch commits in
  the unit's existing test files) x 1000 / src lines, over the same units (a squash- or rebase-merged pull request
  has no branch commits and adds nothing to the second term);
- `radius`: existing test files modified per src-changing unit (median, p90 by nearest rank, max, top three);
- `def test_` lines added/removed in test files over all units, and how many were added in new files.
`hot_test_files`: the existing test files modified by the most units over the last four reported weeks.
JSON is for a reading routine; md is one pipe-separated line per week (header first, starting with `#`) for a journal.
Exit 0 (an empty week is reported with zeros), 2 when the ref is unknown, the clone is shallow, or git's text cannot be
parsed.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from scripts.quality.git_units import (
    EXISTING_STATUSES, LEDGER, SRC, TEST, GitHistoryError, GitParseError, Unit, path_class, read_units,
)

MECHANICAL_PREFIX = "test(mechanical)"
HOT_WEEKS = 4
HOT_FILES = 5
TOP_RADIUS = 3


@dataclass(frozen=True)
class Week:
    start: datetime  # Monday 00:00 UTC

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=7)

    @property
    def label(self) -> str:
        return self.start.date().isoformat()


def report_weeks(weeks: int, until: date) -> list[Week]:
    """The `weeks` complete weeks ending at the latest Monday on or before `until`, oldest first."""
    last_end = datetime.combine(until - timedelta(days=until.weekday()), time(0), tzinfo=timezone.utc)
    return [Week(last_end - timedelta(days=7 * k)) for k in range(weeks, 0, -1)]


def _ratio(num: int, den: int, scale: int = 1) -> float | None:
    return None if den == 0 else num * scale / den


def _changes_src(u: Unit) -> bool:
    return any(path_class(f.path) == SRC for f in u.files)


def _existing(u: Unit, cls: str) -> list[Any]:
    return [f for f in u.files if f.status in EXISTING_STATUSES and path_class(f.path) == cls]


def _mechanical_lines(u: Unit) -> int:
    """Test lines of the unit's `test(mechanical)` branch commits, in files that existed before the unit (M/R in the
    unit's own diff); lines in files the pull request created or deleted, or that its net diff leaves untouched, do
    not count."""
    existing = {f.path for f in _existing(u, TEST)}
    return sum(
        f.lines for c in u.branch_commits if c.subject.startswith(MECHANICAL_PREFIX)
        for f in c.files if f.path in existing
    )


def _p90(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def week_metrics(units: Sequence[Unit]) -> dict[str, Any]:
    src_units = [u for u in units if _changes_src(u)]
    src_lines = sum(f.lines for u in src_units for f in u.files if path_class(f.path) == SRC)
    test_lines = sum(f.lines for u in src_units for f in _existing(u, TEST))
    ledger_lines = sum(f.lines for u in src_units for f in _existing(u, LEDGER))
    mechanical_lines = sum(_mechanical_lines(u) for u in src_units)
    radii = [(len(_existing(u, TEST)), u) for u in src_units]
    values = [r for r, _ in radii]
    top = sorted(radii, key=lambda ru: (-ru[0], ru[1].date))[:TOP_RADIUS]
    test_files = [f for u in units for f in u.files if path_class(f.path) == TEST]
    tests_added = sum(f.test_defs_added for f in test_files)
    in_new = sum(f.test_defs_added for f in test_files if f.status == "A")
    return {
        "units": len(units),
        "units_changing_src": len(src_units),
        "src_lines_changed": src_lines,
        "existing_test_lines_changed": test_lines,
        "drag": _ratio(test_lines, src_lines),
        "existing_ledger_lines_changed": ledger_lines,
        "mechanical_test_lines_changed": mechanical_lines,
        "bookkeeping_per_kloc": _ratio(ledger_lines + mechanical_lines, src_lines, 1000),
        "radius": {
            "median": statistics.median(values) if values else None,
            "p90": _p90(values) if values else None,
            "max": max(values) if values else None,
            "top": [{"pr": u.pr, "sha": u.sha, "title": u.title, "files": r} for r, u in top],
        },
        "tests_added": tests_added,
        "tests_removed": sum(f.test_defs_removed for f in test_files),
        "tests_added_in_new_files": in_new,
        "new_test_files": sum(1 for f in test_files if f.status == "A"),
        "removed_test_files": sum(1 for f in test_files if f.status == "D"),
        "new_file_share": _ratio(in_new, tests_added),
    }


def hot_test_files(units: Sequence[Unit], limit: int = HOT_FILES) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for u in units:
        counts.update({f.path for f in _existing(u, TEST)})
    ranked = sorted(counts.items(), key=lambda pc: (-pc[1], pc[0]))[:limit]
    return [{"path": p, "units": n} for p, n in ranked]


def weekly_friction(units: Sequence[Unit], weeks: int, until: date) -> dict[str, Any]:
    """Pure: the report over hand-built or read units. Units outside the reported weeks are ignored."""
    ws = report_weeks(weeks, until)
    rows = []
    for w in ws:
        in_week = [u for u in units if w.start <= u.date < w.end]
        rows.append({"week": w.label, **week_metrics(in_week)})
    hot = ws[-HOT_WEEKS:]
    hot_units = [u for u in units if hot and hot[0].start <= u.date < hot[-1].end]
    return {"until": until.isoformat(), "weeks": rows, "hot_test_files": hot_test_files(hot_units)}


_MD_COLUMNS = (
    "week", "units", "units_changing_src", "src_lines_changed", "existing_test_lines_changed", "drag",
    "existing_ledger_lines_changed", "mechanical_test_lines_changed", "bookkeeping_per_kloc",
    "radius_median", "radius_p90", "radius_max", "tests_added", "tests_removed", "tests_added_in_new_files",
    "new_test_files", "removed_test_files", "new_file_share",
)
_RATIOS = {"drag", "bookkeeping_per_kloc", "new_file_share"}


def _md_cell(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key in _RATIOS:
        return f"{value:.2f}"
    return f"{value:g}" if isinstance(value, float) else str(value)


def to_markdown(report: dict[str, Any]) -> str:
    lines = ["# " + " | ".join(_MD_COLUMNS)]
    for row in report["weeks"]:
        flat = {**row, **{f"radius_{k}": v for k, v in row["radius"].items()}}
        lines.append(" | ".join(_md_cell(c, flat[c]) for c in _MD_COLUMNS))
    return "\n".join(lines)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--until", type=_date, default=datetime.now(timezone.utc).date())
    parser.add_argument("--format", choices=("json", "md"), default="json")
    args = parser.parse_args(argv)
    ws = report_weeks(args.weeks, args.until)
    try:
        units = read_units(args.ref, ws[0].start, ws[-1].end) if ws else []
    except (GitHistoryError, GitParseError) as exc:
        print(f"friction: {exc}", file=sys.stderr)
        return 2
    report = {"ref": args.ref, **weekly_friction(units, args.weeks, args.until)}
    print(to_markdown(report) if args.format == "md" else json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
