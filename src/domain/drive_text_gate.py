"""
Text-quality gate (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.1 step 6, §5.5 class 3). Pure and deterministic.

Public interface:
    GateMetrics(lines, residue_lines, repetition, near_empty, variety, plain_lines, plain_repetition)
    measure(text) -> GateMetrics
    is_machine_text(text) -> bool

Machine text disguised as text (a server log, a SQL or HAR dump, telemetry saved as `.txt`) would fill the index with
near-identical chunks. Each line first loses a leading timestamp, speaker tag or IP address; a line that is then only
a timestamp or a number is ignored. What remains is the residue. Its metrics are taken over a bounded sample of
residue lines spread evenly over the text: repetition (over residue lines of at least REPEAT_MIN_CHARS, every token
holding a digit folded to one, so log lines that differ only in ids, counters or paths repeat), the share of
near-empty residue lines (fewer than two letters: braces, separators; a line carrying an amount or a date is never
near-empty), and token variety over a fixed-size sample.

Machine text is repetition AND low variety together, or a text of near-empty lines. Repetition alone is what a bank
statement, an invoice or a payroll has too: their lines repeat a template, but merchants, items and accounts keep the
token variety up. A short text has too few tokens for variety to be judged; there repetition is taken over the long
residue lines that carry no amount or date (the plain lines) and is judged alone. A ledger's template lines carry
amounts and drop out, a log line stripped of its timestamp carries none and stays. Meeting transcripts, chat exports and dated journals keep prose after their timestamps and pass.
Markdown table rows are not measured: tables are exempt (§8.3). Too little residue is never judged.

Linear in the text: lines are split once, tokens are whitespace-split and folded one by one, and the patterns (the
leading prefix, the date and amount shapes) only ever run on a bounded head of a line or on a single token.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import islice
from typing import List

MIN_RESIDUE_LINES = 20        # fewer residue lines: not enough evidence, never machine text
REPEAT_MIN_CHARS = 20         # short replies ("ok", "yes") never count as repetition
MAX_REPETITION = 0.5          # share of long residue lines that repeat an earlier one (tokens with digits folded)
MAX_NEAR_EMPTY = 0.6          # share of residue lines with fewer than two letters
SAMPLE_LINES = 2_000          # residue lines the metrics are taken over, spread evenly over the text
LINE_CHARS = 1_000            # characters of a line measured: a longer line is measured by its head
PREFIX_CHARS = 160            # head of a line the leading-prefix pattern looks at
VARIETY_SAMPLE = 2_000        # tokens sampled for variety, spread over the sampled lines
VARIETY_MIN_TOKENS = 400      # fewer tokens: variety is not judged
MIN_VARIETY = 0.04            # distinct / total tokens in the sample (tokens with digits folded): dumps measure
                              # <= 0.015, a 1500-line statement over 200 Zipf merchants >= 0.10, prose ~0.3+
VARIETY_NOT_JUDGED = 1.0      # the variety reported when the sample holds fewer than VARIETY_MIN_TOKENS tokens

_TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,6})?(?:\s?[AaPp][Mm])?"
_DATE = (r"(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4}"
         r"|\d{1,2}/[A-Za-z]{3}/\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,?\s+\d{4})?)")
_STAMP = rf"(?:{_DATE}(?:[T ,]+{_TIME})?(?:Z|[+-]\d{{2}}:?\d{{2}})?|{_TIME}(?:\s*-->\s*{_TIME})?)"
_IP = r"(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})"
_PREFIX = re.compile(
    rf"^\s*(?:"
    rf"[\[(<]\s*{_STAMP}\s*[\])>]"                 # [00:01:23] (12:03) [2026-10-01 12:00:00]
    rf"|{_STAMP}"                                   # 2026-10-01 12:00:00,123 / 00:01:23.456
    rf"|{_IP}"                                      # 192.168.0.1
    rf"|<v\s+[^>]{{1,40}}>"                         # <v Clara> (WebVTT voice)
    rf"|\[[^\]\n]{{1,40}}\]\s*:?"                   # [Clara]
    rf"|[^\W\d_][\w.'’-]*(?:\s+[^\W\d_][\w.'’-]*){{0,2}}\s*:(?!//)"   # Clara: / Dr. Marta Quillen:
    rf")\s*[-–—|:,]*\s*"
)
_PREFIX_MARKS = frozenset(":[(<")   # every prefix holds a digit or one of these: a head with neither never matches
_ONLY_NUMBER = re.compile(r"[\s\d.,:;/+\-–—#%()\[\]]*")
_DIGIT = re.compile(r"\d")
_LETTER = re.compile(r"[^\W\d_]")
_CURRENCY = frozenset("€$£¥₴₽₹₺₩")
_EDGE = "€$£¥₴₽₹₺₩+-−–()[]{}<>\"'*;:.,"   # stripped from a token's ends before its shape is read
_DATE_TOKEN = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}")
_AMOUNT_TOKEN = re.compile(r"\d{1,3}(?:[.,  ']\d{3})+(?:[.,]\d{1,2})?|\d+[.,]\d{1,2}")


@dataclass(frozen=True)
class GateMetrics:
    lines: int                # non-empty lines measured (table rows excluded)
    residue_lines: int        # lines left after stripping, not only a timestamp or number
    repetition: float
    near_empty: float
    variety: float            # 1.0 when not judged (too few tokens)
    plain_lines: int = 0      # long residue lines in the sample with no amount or date
    plain_repetition: float = 0.0   # share of plain lines that repeat an earlier one (tokens with digits folded)


def _residue(line: str) -> str:
    rest = line
    for _ in range(4):   # "[12:00] 10.0.0.1 Clara: hi": several prefixes in a row
        head = rest[:PREFIX_CHARS]
        if not any(ch.isdigit() or ch in _PREFIX_MARKS for ch in head):
            break
        m = _PREFIX.match(head)
        if m is None or m.end() == 0:
            break
        rest = rest[m.end():]
    return rest.strip()


def _fold(token: str) -> str:
    """A token holding a digit is one token, whatever its ids, counters or paths."""
    return "0" if _DIGIT.search(token) else token.casefold()


def _ledger(line: str, tokens: List[str]) -> bool:
    """The line carries an amount (a currency sign with a number, a grouped or a decimal number) or a date."""
    if not _DIGIT.search(line):
        return False
    if any(ch in _CURRENCY for ch in line):
        return True
    for token in tokens:
        core = token.strip(_EDGE)
        if _DATE_TOKEN.fullmatch(core) or _AMOUNT_TOKEN.fullmatch(core):
            return True
    return False


def _near_empty(line: str, tokens: List[str]) -> bool:
    letters = sum(1 for _ in islice(_LETTER.finditer(line), 2))
    return letters < 2 and not _ledger(line, tokens)


def _spread(items: List[str], limit: int) -> List[str]:
    """At most `limit` items, evenly spaced over the list, in order."""
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def _repetition(lines: List[str]) -> float:
    return (len(lines) - len(set(lines))) / len(lines) if lines else 0.0


def measure(text: str) -> GateMetrics:
    measured = 0
    residue: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("|"):
            continue
        measured += 1
        rest = _residue(line[:LINE_CHARS])
        if rest and not _ONLY_NUMBER.fullmatch(rest):
            residue.append(rest)
    if not residue:
        return GateMetrics(measured, 0, 0.0, 0.0, VARIETY_NOT_JUDGED)
    sample = _spread(residue, SAMPLE_LINES)
    split = [r.split() for r in sample]
    folded = [[_fold(t) for t in tokens] for tokens in split]
    long_lines = [" ".join(f) for f, r in zip(folded, sample) if len(r) >= REPEAT_MIN_CHARS]
    repetition = _repetition(long_lines)
    plain = [" ".join(f) for f, r, tokens in zip(folded, sample, split)
             if len(r) >= REPEAT_MIN_CHARS and not _ledger(r, tokens)]
    plain_repetition = _repetition(plain)
    near_empty = sum(1 for r, tokens in zip(sample, split) if _near_empty(r, tokens)) / len(sample)
    total = sum(len(f) for f in folded)
    every = max(1, total // VARIETY_SAMPLE)   # every n-th line: the token sample spans the text, not its head
    tokens: List[str] = []
    for line_tokens in folded[::every]:
        tokens.extend(line_tokens)
        if len(tokens) >= VARIETY_SAMPLE:
            break
    tokens = tokens[:VARIETY_SAMPLE]
    variety = len(set(tokens)) / len(tokens) if len(tokens) >= VARIETY_MIN_TOKENS else VARIETY_NOT_JUDGED
    return GateMetrics(measured, len(residue), repetition, near_empty, variety, len(plain), plain_repetition)


def is_machine_text(text: str) -> bool:
    m = measure(text)
    if m.residue_lines < MIN_RESIDUE_LINES:
        return False
    if m.near_empty > MAX_NEAR_EMPTY:
        return True
    if m.repetition > MAX_REPETITION and m.variety < MIN_VARIETY:
        return True
    # Too few tokens for variety (a log under ~70 lines): the plain lines' repetition is judged alone.
    return (m.variety == VARIETY_NOT_JUDGED and m.plain_lines >= MIN_RESIDUE_LINES
            and m.plain_repetition > MAX_REPETITION)
