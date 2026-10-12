"""
Text-quality gate (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.1 step 6, §5.5 class 3). Pure and deterministic.

Public interface:
    GateMetrics(lines, residue_lines, repetition, near_empty, variety)
    measure(text) -> GateMetrics
    is_machine_text(text) -> bool

Machine text disguised as text (a server log, a SQL or HAR dump saved as `.txt`) would fill the index with
near-identical chunks. Each line first loses a leading timestamp, speaker tag or IP address; a line that is then only
a timestamp or a number is ignored. What remains is the residue, measured three ways: repetition (over residue lines
of at least REPEAT_MIN_CHARS, every token holding a digit folded to one, so log lines that differ only in ids,
counters or paths repeat), the share of
near-empty residue lines (fewer than two letters: braces, separators), and token variety over a fixed-size sample.
Meeting transcripts, chat exports and dated journals keep prose after their timestamps and pass. Markdown table rows
are not measured: tables are exempt (§8.3). Too little residue is never judged machine text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

MIN_RESIDUE_LINES = 20        # fewer residue lines: not enough evidence, never machine text
REPEAT_MIN_CHARS = 20         # short replies ("ok", "yes") never count as repetition
MAX_REPETITION = 0.5          # share of long residue lines that repeat an earlier one (tokens with digits folded)
MAX_NEAR_EMPTY = 0.6          # share of residue lines with fewer than two letters
VARIETY_SAMPLE = 2_000        # tokens sampled for variety
VARIETY_MIN_TOKENS = 400      # fewer tokens: variety is not judged
MIN_VARIETY = 0.12            # distinct / total tokens in the sample (tokens with digits folded); prose is ~0.3+

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
    rf"|<v\s+[^>]{{1,40}}>"                         # <v Alice> (WebVTT voice)
    rf"|\[[^\]\n]{{1,40}}\]\s*:?"                   # [Alice]
    rf"|[^\W\d_][\w.'’-]*(?:\s+[^\W\d_][\w.'’-]*){{0,2}}\s*:(?!//)"   # Alice: / Dr. Bob Smith:
    rf")\s*[-–—|:,]*\s*"
)
_ONLY_NUMBER = re.compile(r"^[\s\d.,:;/+\-–—#%()\[\]]*$")
_TOKEN = re.compile(r"\S+")
_WITH_DIGIT = re.compile(r"\S*\d\S*")
_LETTER = re.compile(r"[^\W\d_]")


@dataclass(frozen=True)
class GateMetrics:
    lines: int                # non-empty lines measured (table rows excluded)
    residue_lines: int        # lines left after stripping, not only a timestamp or number
    repetition: float
    near_empty: float
    variety: float            # 1.0 when not judged (too few tokens)


def _residue(line: str) -> str:
    rest = line
    for _ in range(4):   # "[12:00] 10.0.0.1 Alice: hi": several prefixes in a row
        m = _PREFIX.match(rest)
        if m is None or m.end() == 0:
            break
        rest = rest[m.end():]
    return rest.strip()


def measure(text: str) -> GateMetrics:
    lines = [ln.strip() for ln in text.splitlines()]
    measured = [ln for ln in lines if ln and not ln.startswith("|")]
    residue: List[str] = []
    for line in measured:
        rest = _residue(line)
        if rest and not _ONLY_NUMBER.match(rest):
            residue.append(rest)
    if not residue:
        return GateMetrics(len(measured), 0, 0.0, 0.0, 1.0)
    folded = [_WITH_DIGIT.sub("0", r).casefold() for r in residue]
    long_lines = [f for f, r in zip(folded, residue) if len(r) >= REPEAT_MIN_CHARS]
    repetition = (len(long_lines) - len(set(long_lines))) / len(long_lines) if long_lines else 0.0
    near_empty = sum(1 for r in residue if len(_LETTER.findall(r)) < 2) / len(residue)
    tokens: List[str] = []
    for f in folded:
        tokens.extend(_TOKEN.findall(f))
        if len(tokens) >= VARIETY_SAMPLE:
            break
    tokens = tokens[:VARIETY_SAMPLE]
    variety = len(set(tokens)) / len(tokens) if len(tokens) >= VARIETY_MIN_TOKENS else 1.0
    return GateMetrics(len(measured), len(residue), repetition, near_empty, variety)


def is_machine_text(text: str) -> bool:
    m = measure(text)
    if m.residue_lines < MIN_RESIDUE_LINES:
        return False
    return m.repetition > MAX_REPETITION or m.near_empty > MAX_NEAR_EMPTY or m.variety < MIN_VARIETY
