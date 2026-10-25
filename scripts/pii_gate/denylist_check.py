"""Deterministic personal-data check: added lines against a private denylist of `tier regex` lines.

The denylist never lives in this repository. It comes from `--denylist PATH`, else `PII_DENYLIST_FILE` (a path),
else `PII_DENYLIST` (the content). Format: one rule per line, `hard <regex>` or `soft <regex>`, case-insensitive;
`#` comments and blank lines are ignored.

Output is `file:line tier rule`, where rule is the rule's line number in the denylist. The matched text is never
printed (CI logs are public); a file whose path itself matches is shown as `(file path #N in the change)`.
With `--summary-only` (CI) the per-hit lines are not printed at all: one line gives the counts, no file, line or rule.

Exit: 0 clean or soft hits only, 1 a hard hit, 2 a configuration or git error. With no denylist configured it prints
one notice and exits 0, unless `--require` is given (CI), which makes that exit 2.

    python3 -m scripts.pii_gate.denylist_check --staged
    python3 -m scripts.pii_gate.denylist_check --range origin/main...HEAD
    python3 -m scripts.pii_gate.denylist_check --files $(git ls-files)
    python3 -m scripts.pii_gate.denylist_check --message .git/COMMIT_EDITMSG
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.pii_gate.added_lines import AddedLine, commit_messages, files, in_range, staged  # noqa: E402

TIERS = ("hard", "soft")
NO_CONFIG_NOTICE = ("PII denylist: not configured (set PII_DENYLIST_FILE, PII_DENYLIST or pass --denylist); "
                    "check skipped.")


class ConfigError(Exception):
    """The denylist is unreadable or malformed. The message never contains a pattern."""


@dataclass(frozen=True)
class Rule:
    number: int
    tier: str
    regex: re.Pattern[str]


def load_rules(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0] not in TIERS:
            raise ConfigError(f"denylist line {number}: expected '<hard|soft> <regex>'")
        try:
            rules.append(Rule(number, parts[0], re.compile(parts[1], re.IGNORECASE)))
        except re.error as exc:
            raise ConfigError(f"denylist line {number}: invalid regex ({exc.msg})") from None
    return rules


def denylist_text(explicit: str | None) -> str | None:
    """The denylist content from the flag or the environment; None when nothing is configured."""
    path = explicit or os.environ.get("PII_DENYLIST_FILE") or None
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"denylist file unreadable ({exc.strerror})") from None
    return os.environ.get("PII_DENYLIST") or None


@dataclass(frozen=True)
class Hit:
    where: str
    tier: str
    rule: int


def scan(lines: list[AddedLine], paths: list[str], rules: list[Rule]) -> list[Hit]:
    withheld: dict[str, str] = {}
    hits: list[Hit] = []
    for index, path in enumerate(paths, 1):
        for rule in rules:
            if rule.regex.search(path):
                withheld.setdefault(path, f"(file path #{index} in the change)")
                hits.append(Hit(withheld[path], rule.tier, rule.number))
    for item in lines:
        shown = withheld.get(item.path, item.path)
        for rule in rules:
            if rule.regex.search(item.text):
                hits.append(Hit(f"{shown}:{item.line}", rule.tier, rule.number))
    return hits


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check added lines against the private personal-data denylist.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--staged", action="store_true", help="the staged diff's added lines")
    source.add_argument("--range", metavar="A..B", help="a diff range (A...B = what B adds since the merge base); "
                                                         "also checks the range's commit messages")
    source.add_argument("--files", nargs="+", metavar="FILE", help="every line of these files")
    source.add_argument("--message", metavar="FILE", help="a commit message file (commit-msg hook)")
    parser.add_argument("--denylist", metavar="PATH", help="denylist file (else PII_DENYLIST_FILE / PII_DENYLIST)")
    parser.add_argument("--summary-only", action="store_true",
                        help="print the counts only, no file, line or rule per hit (CI: the log is public)")
    parser.add_argument("--require", action="store_true", help="a missing or empty denylist is an error (CI)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        text = denylist_text(args.denylist)
        rules = load_rules(text) if text is not None else []
    except ConfigError as exc:
        print(f"PII denylist: {exc}", file=sys.stderr)
        return 2
    if not rules:
        if args.require:
            print("PII denylist: no rules configured and --require is set.", file=sys.stderr)
            return 2
        print(NO_CONFIG_NOTICE)
        return 0

    try:
        if args.staged:
            change = staged()
            lines, paths = change.lines, change.paths
        elif args.range:
            change = in_range(args.range)
            lines, paths = change.lines + commit_messages(args.range), change.paths
        elif args.files:
            change = files(args.files)
            lines, paths = change.lines, change.paths
        else:
            body = Path(args.message).read_text(encoding="utf-8", errors="replace")
            lines = [AddedLine("commit message", no, t) for no, t in enumerate(body.splitlines(), 1)
                     if not t.startswith("#")]
            paths = []
    except (RuntimeError, OSError) as exc:
        print(f"PII denylist: cannot read the change: {exc}", file=sys.stderr)
        return 2

    hits = scan(lines, paths, rules)
    hard = [h for h in hits if h.tier == "hard"]
    soft = [h for h in hits if h.tier == "soft"]
    if args.summary_only:
        if hits:
            print(f"PII denylist: {len(hard)} hard, {len(soft)} soft over {len(lines)} lines "
                  "(details withheld; run the check locally without --summary-only).")
    else:
        for hit in hard + soft:
            print(f"{hit.where} {hit.tier} {hit.rule}")
        if hits:
            print(f"PII denylist: {len(hard)} hard, {len(soft)} soft over {len(lines)} lines "
                  "(rule = line number in the denylist; the matched text is not shown).")
    if hard:
        print("PII denylist: BLOCKED. Replace the private detail with one from the invented cast "
              "(tests/CLAUDE.md), or rewrite it.", file=sys.stderr)
        return 1
    if soft:
        print("PII denylist: soft hits are warnings; look at each line before you commit.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
