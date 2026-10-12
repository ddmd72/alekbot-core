"""
Content exact-token stream keys (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.6). Pure; one normaliser for indexing
and for the query, so `1 250,00 €` in a scan and `1250.00` in a question meet. A change bumps EXTRACTION_VERSION.

Public interface:
    content_terms(text) -> Tuple[str, ...]   sorted, unique, at most CONTENT_TERMS_PER_CHUNK
    query_terms(query) -> Tuple[str, ...]    the query's terms, most specific first

Terms:
    amounts   "1 250,00" "1.250,00" "1,250.00" "1250.5"  -> "1250.00" / "1250.50" and the integer part "1250"
    dates     "15.03.2024" "03/15/2024" "2024-03-15"     -> "2024-03-15" (both readings when day and month are <= 12)
    ids       "INV-2024/0012"                             -> "inv20240012", "20240012", "2024", "0012"
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, List, Set, Tuple

CONTENT_TERMS_PER_CHUNK = 256
_MIN_DIGITS = 3

_SEP = r"[   '.,]"
# grouped amounts (1 250,00 / 1.250,00 / 1,250.00) and plain decimals (1250,5 / 1250.50)
_AMOUNT = re.compile(rf"(?<![\w.,])(\d{{1,3}}(?:{_SEP}\d{{3}})+(?:[.,]\d{{1,2}})?|\d+[.,]\d{{1,2}})(?![\w]|[.,]\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_ID = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ]+(?:[-/_.][0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ]+)*")
_DIGITS = re.compile(r"\d+")


def _amount(raw: str) -> List[str]:
    compact = re.sub(r"[   ']", "", raw)
    m = re.fullmatch(r"(\d[\d.,]*?)(?:([.,])(\d{1,2}))?", compact)
    if m is None:
        return []
    whole = re.sub(r"[.,]", "", m.group(1))
    if not whole.isdigit():
        return []
    whole = str(int(whole))
    if m.group(3) is None:
        return [whole] if len(whole) >= _MIN_DIGITS else []
    cents = m.group(3).ljust(2, "0")
    return [f"{whole}.{cents}", whole] if len(whole) >= _MIN_DIGITS else [f"{whole}.{cents}"]


def _iso(y: int, m: int, d: int) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def _dates(text: str) -> Iterable[str]:
    for m in _YMD.finditer(text):
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            yield iso
    for m in _DMY.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for iso in {_iso(y, b, a), _iso(y, a, b)}:   # day-first and month-first: one is valid, or both are
            if iso:
                yield iso


def _ids(text: str) -> Iterable[str]:
    for m in _ID.finditer(text):
        token = m.group(0)
        if not any(ch.isdigit() for ch in token):
            continue
        runs = _DIGITS.findall(token)
        joined = re.sub(r"[-/_.]", "", token).casefold()
        if len(joined) >= _MIN_DIGITS and not joined.isdigit():
            yield joined
        digits = "".join(runs)
        if len(digits) >= _MIN_DIGITS:
            yield digits
        for run in runs:
            if len(run) >= _MIN_DIGITS:
                yield run


def _terms(text: str) -> Set[str]:
    terms: Set[str] = set(_dates(text))
    for m in _AMOUNT.finditer(text):
        terms.update(_amount(m.group(1)))
    terms.update(_ids(text))
    return terms


def content_terms(text: str) -> Tuple[str, ...]:
    """A chunk's terms. Bounded: past the cap, the longest (most specific) terms are kept."""
    terms = _terms(text)
    if len(terms) > CONTENT_TERMS_PER_CHUNK:
        terms = set(sorted(terms, key=lambda t: (-len(t), t))[:CONTENT_TERMS_PER_CHUNK])
    return tuple(sorted(terms))


def query_terms(query: str) -> Tuple[str, ...]:
    """The query's terms, most specific (longest) first; the search queries at most CONTENT_TERM_QUERIES of them."""
    return tuple(sorted(_terms(query), key=lambda t: (-len(t), t)))
