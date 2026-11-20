"""The one place that reads git for the quality metrics (friction.py, fix_units.py).

A *unit* is one commit on the first-parent chain of a ref. A merge commit's change is its diff against its FIRST
parent (the whole pull request), a plain commit's against its parent. Renames are detected (`-M`); a binary file
(numstat `-`) counts 0 lines.

Layout: `run_git` is the only subprocess call; the parsers take git's text and return frozen dataclasses (pure,
tested on hand-written text); the readers are thin: they call `run_git` and a parser. Read-only, standard library
plus the `git` binary. A missing ref or a shallow history that does not reach the window raises `GitHistoryError`
(the CLIs exit 2 on it: a silent partial week is the one failure a reader of the numbers cannot see).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable, Sequence

_REC = "\x1e"  # record separator: starts every commit
_FLD = "\x1f"  # field separator inside the header
# sha, parents, committer time (unix), subject, body. -z ends the header with NUL, then the raw and numstat entries.
_LOG_FORMAT = f"--format={_REC}%H{_FLD}%P{_FLD}%ct{_FLD}%s{_FLD}%b{_FLD}"

_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) from ")
_TRAILING_PR = re.compile(r"\(#(\d+)\)\s*$")
_TEST_DEF = re.compile(r"^\s*(async\s+)?def test_")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_SHA = re.compile(r"^[0-9a-f]{40} \d+ \d+")

SRC, TEST, LEDGER, OTHER = "src", "test", "ledger", "other"
EXISTING_STATUSES = frozenset({"M", "R"})


class GitHistoryError(Exception):
    """The ref does not exist, or the history is too shallow to cover the asked window."""


# --------------------------------------------------------------------------------------------------- records


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A, M, D, R (renamed; similarity dropped), T, C, ...
    added: int
    deleted: int
    old_path: str | None = None  # the pre-image path of a rename/copy
    test_defs_added: int = 0  # added diff lines matching `def test_` (filled for test files by the patch reader)
    test_defs_removed: int = 0

    @property
    def lines(self) -> int:
        return self.added + self.deleted


@dataclass(frozen=True)
class BranchCommit:
    sha: str
    subject: str
    files: tuple[FileChange, ...]


@dataclass(frozen=True)
class Unit:
    sha: str
    date: datetime  # committer date, UTC
    title: str
    pr: int | None
    files: tuple[FileChange, ...]
    branch_commits: tuple[BranchCommit, ...] = ()
    parents: tuple[str, ...] = ()

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(frozen=True)
class Hunk:
    """One old-side range of a `-U0` diff; `count == 0` is a pure addition."""

    start: int
    count: int


@dataclass(frozen=True)
class FilePatch:
    path: str  # post-image path ("" when the file was deleted)
    old_path: str  # pre-image path ("" when the file was added)
    hunks: tuple[Hunk, ...] = ()
    test_defs_added: int = 0
    test_defs_removed: int = 0


@dataclass(frozen=True)
class BlameLine:
    sha: str
    committer_time: int


@dataclass(frozen=True)
class _RawCommit:
    sha: str
    parents: tuple[str, ...]
    time: int
    subject: str
    body: str
    files: tuple[FileChange, ...] = field(default=())


# --------------------------------------------------------------------------------------------------- path classes


def path_class(path: str) -> str:
    """`src` (src/**/*.py, root main.py and *_main.py), `test` (tests/**/*.py), `ledger` (tests/**/*.txt outside
    tests/fixtures/), else `other`."""
    if path.endswith(".py"):
        if path.startswith("src/"):
            return SRC
        if "/" not in path and (path == "main.py" or path.endswith("_main.py")):
            return SRC
        if path.startswith("tests/"):
            return TEST
    if path.startswith("tests/") and path.endswith(".txt") and not path.startswith("tests/fixtures/"):
        return LEDGER
    return OTHER


def unit_title(subject: str, body: str) -> tuple[str, int | None]:
    """(title, pr). A GitHub merge takes its title from the first non-empty body line; else the subject and a
    trailing `(#n)`."""
    m = _MERGE_SUBJECT.match(subject)
    if m:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), subject)
        return first, int(m.group(1))
    t = _TRAILING_PR.search(subject)
    return subject, (int(t.group(1)) if t else None)


# --------------------------------------------------------------------------------------------------- parsers


def _int(value: str) -> int:
    return 0 if value == "-" else int(value)  # numstat prints `-` for a binary file


def _parse_changes(rest: str) -> tuple[FileChange, ...]:
    """The `--raw --numstat -z` part of one commit: raw entries give the status, numstat entries the counts."""
    tokens = [t.lstrip("\n") for t in rest.split("\0")]
    status: dict[str, tuple[str, str | None]] = {}
    counts: dict[str, tuple[int, int]] = {}
    order: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if tok.startswith(":"):
            code = tok.split()[-1]
            if code[0] in "RC":
                old, new = tokens[i], tokens[i + 1]
                i += 2
                status[new] = (code[0], old)
            else:
                new = tokens[i]
                i += 1
                status[new] = (code[0], None)
            if new not in order:
                order.append(new)
            continue
        parts = tok.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if path == "":  # rename/copy: the two paths follow as their own tokens
            path = tokens[i + 1]
            i += 2
        counts[path] = (_int(added), _int(deleted))
        if path not in order:
            order.append(path)
    out = []
    for path in order:
        code, old_path = status.get(path, ("M", None))
        a, d = counts.get(path, (0, 0))
        out.append(FileChange(path=path, status=code, added=a, deleted=d, old_path=old_path))
    return tuple(out)


def _parse_raw_log(text: str) -> list[_RawCommit]:
    commits = []
    for record in text.split(_REC):
        if _FLD not in record:
            continue
        sha, parents, time, subject, body, rest = record.split(_FLD, 5)
        commits.append(_RawCommit(
            sha=sha.strip(), parents=tuple(parents.split()), time=int(time), subject=subject,
            body=body, files=_parse_changes(rest),
        ))
    return commits


def parse_log(text: str) -> list[Unit]:
    """Units from `git log` run with `_LOG_FORMAT -z --raw --numstat` (branch commits are attached by the reader)."""
    units = []
    for c in _parse_raw_log(text):
        title, pr = unit_title(c.subject, c.body)
        units.append(Unit(
            sha=c.sha, date=datetime.fromtimestamp(c.time, tz=timezone.utc), title=title, pr=pr,
            files=c.files, parents=c.parents,
        ))
    return units


def parse_branch_commits(text: str) -> tuple[BranchCommit, ...]:
    """Branch commits from the same `git log` format (`first..second --no-merges`)."""
    return tuple(BranchCommit(sha=c.sha, subject=c.subject, files=c.files) for c in _parse_raw_log(text))


def _strip_prefix(path: str, prefix: str) -> str:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == "/dev/null":
        return ""
    return path[len(prefix):] if path.startswith(prefix) else path


@dataclass
class _PatchBuilder:
    path: str
    old_path: str
    hunks: list[Hunk] = field(default_factory=list)
    added: int = 0
    removed: int = 0

    def build(self) -> FilePatch:
        return FilePatch(path=self.path, old_path=self.old_path, hunks=tuple(self.hunks),
                         test_defs_added=self.added, test_defs_removed=self.removed)


def parse_hunks(text: str) -> list[FilePatch]:
    """One unified diff (`-U0` is enough): old-side ranges and `def test_` lines added/removed, per file."""
    builders: list[_PatchBuilder] = []
    in_header = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            old, _, new = line[len("diff --git "):].partition(" b/")
            builders.append(_PatchBuilder(path=new, old_path=_strip_prefix(old, "a/")))
            in_header = True
            continue
        if not builders:
            continue
        cur = builders[-1]
        m = _HUNK.match(line)
        if m:
            in_header = False
            count = 1 if m.group(2) is None else int(m.group(2))
            cur.hunks.append(Hunk(start=int(m.group(1)), count=count))
        elif in_header:
            if line.startswith("--- "):
                cur.old_path = _strip_prefix(line[4:], "a/")
            elif line.startswith("+++ "):
                cur.path = _strip_prefix(line[4:], "b/")
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur.path = line[len("rename to "):]
            elif line.startswith("new file mode"):
                cur.old_path = ""
            elif line.startswith("deleted file mode"):
                cur.path = ""
        elif line.startswith("+") and _TEST_DEF.match(line[1:]):
            cur.added += 1
        elif line.startswith("-") and _TEST_DEF.match(line[1:]):
            cur.removed += 1
    return [b.build() for b in builders]


def parse_patch_log(text: str) -> dict[str, list[FilePatch]]:
    """`git log -p` run with `--format=<RS>%H`: the file patches of each commit, by sha."""
    out: dict[str, list[FilePatch]] = {}
    for record in text.split(_REC):
        if not record.strip():
            continue
        sha, _, diff = record.partition("\n")
        out[sha.strip()] = parse_hunks(diff)
    return out


def parse_blame(text: str) -> list[BlameLine]:
    """`git blame --porcelain`: one entry per blamed line, with its commit's committer time."""
    times: dict[str, int] = {}
    lines: list[str] = []
    current = ""
    for line in text.split("\n"):
        if _SHA.match(line):
            current = line[:40]
        elif line.startswith("\t"):
            lines.append(current)
        elif line.startswith("committer-time "):
            times[current] = int(line.split()[1])
    return [BlameLine(sha=s, committer_time=times.get(s, 0)) for s in lines]


def with_test_defs(units: Sequence[Unit], patches: dict[str, list[FilePatch]]) -> list[Unit]:
    """Fill each test file's `def test_` counts from the patch reader's output."""
    out = []
    for u in units:
        by_path: dict[str, FilePatch] = {}
        for p in patches.get(u.sha, []):
            by_path[p.path or p.old_path] = p
        files = tuple(
            replace(f, test_defs_added=by_path[f.path].test_defs_added,
                    test_defs_removed=by_path[f.path].test_defs_removed)
            if f.path in by_path else f
            for f in u.files
        )
        out.append(replace(u, files=files))
    return out


# --------------------------------------------------------------------------------------------------- readers


def run_git(args: Sequence[str], cwd: str | None = None) -> str:
    """The only subprocess call of the quality scripts."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True, encoding="utf-8", errors="replace",
    ).stdout


def check_history(ref: str, since: datetime, cwd: str | None = None) -> None:
    """Raise GitHistoryError when `ref` is unknown, or the clone is shallow and its first-parent chain does not
    reach back before `since`."""
    try:
        run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    except subprocess.CalledProcessError as exc:
        raise GitHistoryError(f"unknown ref: {ref}") from exc
    if run_git(["rev-parse", "--is-shallow-repository"], cwd).strip() != "true":
        return
    times = run_git(["log", "--first-parent", "--format=%ct", ref], cwd).split()
    oldest = int(times[-1]) if times else 0
    if oldest >= since.timestamp():
        when = datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat()
        raise GitHistoryError(
            f"shallow history: the oldest reachable commit of {ref} ({when}) does not precede {since.isoformat()}"
        )


def read_units(ref: str, since: datetime, until: datetime, *, branch_commits: bool = True,
               cwd: str | None = None) -> list[Unit]:
    """The units of `ref` with since <= committer date < until (UTC), newest first, test-def counts filled."""
    check_history(ref, since, cwd)
    margin = 2 * 86400  # git's own --since/--until are a coarse pre-filter; the window is applied exactly below
    window = [f"--since=@{int(since.timestamp()) - margin}", f"--until=@{int(until.timestamp()) + margin}"]
    text = run_git(["log", "--first-parent", "--diff-merges=first-parent", "-M", "--raw", "--numstat", "-z",
                    _LOG_FORMAT, *window, ref], cwd)
    units = [u for u in parse_log(text) if since <= u.date < until]
    if not units:
        return []
    patch_text = run_git(["log", "--first-parent", "--diff-merges=first-parent", "-M", "-U0", "--no-color",
                          f"--format={_REC}%H", *window, ref, "--", "tests/"], cwd)
    units = with_test_defs(units, parse_patch_log(patch_text))
    if branch_commits:
        units = [replace(u, branch_commits=read_branch_commits(u, cwd)) if u.is_merge else u for u in units]
    return units


def read_branch_commits(unit: Unit, cwd: str | None = None) -> tuple[BranchCommit, ...]:
    text = run_git(["log", "--no-merges", "-M", "--raw", "--numstat", "-z", _LOG_FORMAT,
                    f"{unit.parents[0]}..{unit.parents[1]}"], cwd)
    return parse_branch_commits(text)


def read_hunks(base: str, head: str, paths: Iterable[str], cwd: str | None = None) -> list[FilePatch]:
    paths = list(paths)
    if not paths:
        return []
    return parse_hunks(run_git(["diff", "-M", "-U0", "--no-color", base, head, "--", *paths], cwd))


def read_blame(rev: str, path: str, hunks: Sequence[Hunk], cwd: str | None = None) -> list[BlameLine]:
    ranges = [arg for h in hunks if h.count > 0 for arg in ("-L", f"{h.start},+{h.count}")]
    if not ranges:
        return []
    return parse_blame(run_git(["blame", "--porcelain", *ranges, rev, "--", path], cwd))


def read_first_parent_chain(ref: str, cwd: str | None = None) -> list[str]:
    """The first-parent shas of `ref`, oldest first."""
    return list(reversed(run_git(["rev-list", "--first-parent", ref], cwd).split()))


def read_descendants(commit: str, ref: str, cwd: str | None = None) -> set[str]:
    """Every commit of `ref` that has `commit` as an ancestor. `--first-parent` is deliberately absent: combined with
    `--ancestry-path` it misses a commit brought in by a merge's second parent."""
    return set(run_git(["rev-list", "--ancestry-path", f"{commit}..{ref}"], cwd).split())


def read_commit(sha: str, cwd: str | None = None) -> Unit:
    """One commit's header as a Unit (no files)."""
    text = run_git(["show", "-s", "-z", _LOG_FORMAT, sha], cwd)
    return parse_log(text)[0]
