"""The added lines of a change, with their file and line number: what both personal-data checks read.

Stdlib only. Sources: the staged diff, a diff range, a unified diff text, files, and the commit messages of a range.
"""

from __future__ import annotations

import codecs
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Never read: the bundled gazetteer (real place names by design) and lockfiles (hashes, no prose).
SKIP_PREFIXES = ("src/assets/",)
LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
})

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_DIFF = ("-c", "core.quotePath=false", "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv",
             "--find-renames")


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    text: str


@dataclass
class Change:
    """Added lines plus every path the change writes (new, modified, renamed or copied to)."""

    lines: list[AddedLine] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


def is_skipped(path: str) -> bool:
    return path.startswith(SKIP_PREFIXES) or Path(path).name in LOCKFILES


def _unquote(raw: str) -> str:
    raw = raw.rstrip("\n")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    return raw


def _strip_prefix(path: str) -> str | None:
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_unified_diff(text: str) -> Change:
    """Added lines of a `git diff --unified=0` text. Binary files and skipped paths yield no lines."""
    change = Change()
    path: str | None = None
    old_left = new_left = 0
    new_no = 0
    for raw in text.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith("-") and old_left > 0:
                old_left -= 1
                continue
            if raw.startswith("+") and new_left > 0:
                if path is not None and not is_skipped(path):
                    change.lines.append(AddedLine(path, new_no, raw[1:]))
                new_no += 1
                new_left -= 1
                continue
            if raw.startswith("\\"):
                continue
            old_left = new_left = 0
        if raw.startswith("\\"):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_no = int(hunk.group(2))
            new_left = int(hunk.group(3)) if hunk.group(3) is not None else 1
            continue
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ "):
            path = _strip_prefix(_unquote(raw[4:]))
            _add_path(change, path)
        elif raw.startswith(("rename to ", "copy to ")):
            _add_path(change, _unquote(raw.split(" to ", 1)[1]))
    return change


def _add_path(change: Change, path: str | None) -> None:
    if path is not None and not is_skipped(path) and path not in change.paths:
        change.paths.append(path)


def _git(args: list[str]) -> str:
    out = subprocess.run(["git", *args], capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[-1] if args else ''} failed: {out.stderr.decode('utf-8', 'replace').strip()}")
    return out.stdout.decode("utf-8", "replace")


def staged() -> Change:
    return parse_unified_diff(_git([*_GIT_DIFF, "--cached"]))


def in_range(spec: str) -> Change:
    """`A..B` diffs A against B; `A...B` diffs the merge base against B (what a pull request adds)."""
    return parse_unified_diff(_git([*_GIT_DIFF, spec]))


def commit_messages(spec: str) -> list[AddedLine]:
    """Every line of every commit message in the range, as `commit <sha12>` pseudo-files."""
    log_range = spec.replace("...", "..")
    out = _git(["log", "--format=%H%n%B%x00", log_range])
    lines: list[AddedLine] = []
    for entry in out.split("\x00"):
        entry = entry.lstrip("\n")
        if not entry:
            continue
        sha, _, body = entry.partition("\n")
        for no, text in enumerate(body.splitlines(), 1):
            lines.append(AddedLine(f"commit {sha[:12]}", no, text))
    return lines


def files(paths: list[str]) -> Change:
    """Every line of the given files; binary files (a NUL in the first 8 KiB) and skipped paths are left out."""
    change = Change()
    for name in paths:
        if is_skipped(name):
            continue
        data = Path(name).read_bytes()
        if b"\x00" in data[:8192]:
            continue
        _add_path(change, name)
        for no, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            change.lines.append(AddedLine(name, no, text))
    return change
