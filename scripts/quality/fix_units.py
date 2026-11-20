"""Facts about each merged fix, for a routine that classifies escaped defects.

Usage: python -m scripts.quality.fix_units [--ref origin/main] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit N]

A fix unit is a first-parent unit (git_units.py) whose title starts with `fix` (any case) followed by `(`, `:`, `!` or
whitespace, and that changes at least one src file. The window is half-open in UTC, [since 00:00, until 00:00);
defaults: until = the last Monday on or before today, since = seven days before it (friction's week). `--limit N`
keeps the N oldest.
Per fix: what it touched (`layers`, `src_lines_changed`), whether it came with a test (`tests_added`,
`existing_test_files_modified`, `has_regression_test`), and `fixed_lines_age_days`: the src lines the fix deleted or
replaced, blamed on its first parent; `min_age_days` is the age at the fix's date of the most recent commit among
them, `max_age_days` of the oldest. `min_age_on_main_days` / `max_age_on_main_days` count instead from the date of the
first-parent unit that brought each blamed commit to the ref (a line written on a branch ten days before its merge
and fixed a day after the merge is one day on main): small, the fix follows a recent change; large, the defect lived
on the ref for long. An add-only fix has null ages. `introduced_by`: up to three first-parent units of the ref that brought
in the blamed commits, by blamed lines. Exit 0 (an empty list when the window has no fix), 2 when the ref is unknown,
the clone is shallow, or git's text cannot be parsed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Sequence

from scripts.quality.git_units import (
    EXISTING_STATUSES, SRC, TEST, BlameLine, GitHistoryError, GitParseError, Unit, path_class, read_blame, read_commit,
    read_descendants, read_first_parent_chain, read_hunks, read_units,
)

_FIX_TITLE = re.compile(r"^fix[(:!\s]", re.IGNORECASE)
INTRODUCED_BY = 3


def is_fix_title(title: str) -> bool:
    return bool(_FIX_TITLE.match(title))


def is_fix_unit(unit: Unit) -> bool:
    return is_fix_title(unit.title) and any(path_class(f.path) == SRC for f in unit.files)


def layer_of(path: str) -> str:
    """`src/<dir>` for a file under a src subdirectory, `src` for one directly in src/, `entry` for a root entry
    point."""
    parts = path.split("/")
    if parts[0] != "src":
        return "entry"
    return f"src/{parts[1]}" if len(parts) > 2 else "src"


def blame_ages(lines: Sequence[BlameLine], at: datetime) -> dict[str, Any]:
    if not lines:
        return {"min_age_days": None, "max_age_days": None, "blamed_lines": 0}
    def days(t: int) -> float:
        return round((at.timestamp() - t) / 86400, 1)

    newest = max(ln.committer_time for ln in lines)
    oldest = min(ln.committer_time for ln in lines)
    return {"min_age_days": days(newest), "max_age_days": days(oldest), "blamed_lines": len(lines)}


def on_main_ages(lines: Sequence[BlameLine], at: datetime, unit_of: Callable[[str], Unit]) -> dict[str, Any]:
    """Ages counted from the date each blamed commit's first-parent unit reached the ref; null for an add-only fix."""
    if not lines:
        return {"min_age_on_main_days": None, "max_age_on_main_days": None}
    landed = [unit_of(sha).date for sha in {ln.sha for ln in lines}]
    return {"min_age_on_main_days": round((at - max(landed)).total_seconds() / 86400, 1),
            "max_age_on_main_days": round((at - min(landed)).total_seconds() / 86400, 1)}


def introduced_by(lines: Sequence[BlameLine], unit_of: Callable[[str], Unit]) -> list[dict[str, Any]]:
    """The first-parent units that brought in the blamed lines' commits, most lines first (ties: oldest first)."""
    per_commit = Counter(ln.sha for ln in lines)
    per_unit: Counter[str] = Counter()
    units: dict[str, Unit] = {}
    for sha, n in per_commit.items():
        u = unit_of(sha)
        units[u.sha] = u
        per_unit[u.sha] += n
    ranked = sorted(per_unit, key=lambda s: (-per_unit[s], units[s].date))[:INTRODUCED_BY]
    return [{"sha": s, "date": units[s].date.isoformat(), "title": units[s].title} for s in ranked]


def fix_record(unit: Unit, blamed: Sequence[BlameLine], introducers: list[dict[str, Any]],
               on_main: dict[str, Any] | None = None) -> dict[str, Any]:
    src = [f for f in unit.files if path_class(f.path) == SRC]
    tests = [f for f in unit.files if path_class(f.path) == TEST]
    tests_added = sum(f.test_defs_added for f in tests)
    return {
        "sha": unit.sha,
        "date": unit.date.isoformat(),
        "pr": unit.pr,
        "title": unit.title,
        "layers": sorted({layer_of(f.path) for f in src}),
        "src_lines_changed": sum(f.lines for f in src),
        "tests_added": tests_added,
        "existing_test_files_modified": sum(1 for f in tests if f.status in EXISTING_STATUSES),
        "has_regression_test": tests_added > 0,
        "fixed_lines_age_days": blame_ages(blamed, unit.date)
        | (on_main or {"min_age_on_main_days": None, "max_age_on_main_days": None}),
        "introduced_by": introducers,
    }


def select_fixes(units: Sequence[Unit], limit: int | None) -> list[Unit]:
    fixes = sorted((u for u in units if is_fix_unit(u)), key=lambda u: u.date)
    return fixes[:limit] if limit is not None else fixes


class _UnitResolver:
    """Maps a commit to the first-parent unit of `ref` that brought it in: the commit itself when it is on the
    first-parent chain, else the oldest first-parent commit that has it as an ancestor."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        self.index: dict[str, int] = {}
        self.cache: dict[str, Unit] = {}

    def __call__(self, sha: str) -> Unit:
        if not self.index:  # read on first use: a window without a blamed line needs no chain
            self.index = {s: i for i, s in enumerate(read_first_parent_chain(self.ref))}
        if sha not in self.cache:
            target = sha
            if sha not in self.index:
                on_chain = [s for s in read_descendants(sha, self.ref) if s in self.index]
                target = min(on_chain, key=self.index.__getitem__) if on_chain else sha
            self.cache[sha] = read_commit(target)
        return self.cache[sha]


def blamed_lines(unit: Unit) -> list[BlameLine]:
    """The fix's deleted or replaced src lines, blamed on its first parent."""
    if not unit.parents:
        return []
    paths: list[str] = []
    for f in unit.files:
        if path_class(f.path) == SRC:
            paths.append(f.path)
            if f.old_path:
                paths.append(f.old_path)
    lines: list[BlameLine] = []
    for patch in read_hunks(unit.parents[0], unit.sha, paths):
        if patch.old_path and path_class(patch.old_path) == SRC:
            lines.extend(read_blame(unit.parents[0], patch.old_path, patch.hunks))
    return lines


def _day(value: str) -> date:
    return date.fromisoformat(value)


def default_window(today: date) -> tuple[date, date]:
    """(since, until): the last complete Monday-to-Monday week on or before `today`."""
    until = today - timedelta(days=today.weekday())
    return until - timedelta(days=7), until


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--since", type=_day, default=None)
    parser.add_argument("--until", type=_day, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    until = args.until or default_window(datetime.now(timezone.utc).date())[1]
    since = args.since or until - timedelta(days=7)
    try:
        units = read_units(args.ref, _midnight(since), _midnight(until), branch_commits=False)
        fixes = select_fixes(units, args.limit)
        resolver = _UnitResolver(args.ref)
        records = []
        for u in fixes:
            blamed = blamed_lines(u)
            records.append(fix_record(u, blamed, introduced_by(blamed, resolver),
                                      on_main_ages(blamed, u.date, resolver)))
    except (GitHistoryError, GitParseError) as exc:
        print(f"fix_units: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ref": args.ref, "since": since.isoformat(), "until": until.isoformat(),
                      "fix_units": records}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
