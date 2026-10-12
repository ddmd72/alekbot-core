"""
Content exact-token stream keys (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.6). Pure; one normaliser for indexing
and for the query, so `1 250,00 €` in a scan and `1250.00` in a question meet. A change bumps EXTRACTION_VERSION.

Public interface:
    content_terms(text) -> Tuple[str, ...]   sorted, unique, at most CONTENT_TERMS_PER_CHUNK
    query_terms(query) -> Tuple[str, ...]    the query's maximal forms, most specific first

Terms (the index side keeps every form; the query only the maximal form of each span, review D-M1):
    amounts   "1 250,00" "1.250,00" "1,250.00" "1250.5"  -> "1250.00" / "1250.50"; index also the integer part "1250"
    dates     "15.03.2024" "03/15/2024" "2024-03-15"     -> "2024-03-15" (both readings when day and month are <= 12)
    ids       "INV-2024/0012"                             -> "inv20240012"; index also "20240012", "2024", "0012"
    groups    "XX00 1234 5678 …" "+34 600 000 000"        -> "xx0012345678…", "34600000000" (_GROUP_MIN_DIGITS on)
A token holding `.` or `,` never yields its digits joined, on either side: "1250.00" is not "125000".
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, List, Set, Tuple

CONTENT_TERMS_PER_CHUNK = 256
_MIN_DIGITS = 3
_GROUP_MIN_DIGITS = 9   # whitespace-separated digit groups (phones, IBANs) are joined from this many digits on

_SPACE = "   "   # space, no-break space, narrow no-break space
_SEP = f"[{_SPACE}'.,]"
# grouped amounts (1 250,00 / 1.250,00 / 1,250.00) and plain decimals (1250,5 / 1250.50)
_AMOUNT = re.compile(rf"(?<![\w.,])(\d{{1,3}}(?:{_SEP}\d{{3}})+(?:[.,]\d{{1,2}})?|\d+[.,]\d{{1,2}})(?![\w]|[.,]\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_ID = re.compile(r"[0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ]+(?:[-/_.][0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ]+)*")
_DIGITS = re.compile(r"\d+")
# Runs of short alphanumeric groups that each hold a digit, one space apart: an IBAN, a phone number. Never inside a
# longer token, and never ending where a decimal part or an id separator follows (that is an amount or an id).
_GROUP = r"[A-Za-z]{0,2}\d[0-9A-Za-z]{0,3}(?![0-9A-Za-z])"
_GROUPS = re.compile(rf"(?<![\w.,/+-])\+?{_GROUP}(?:[{_SPACE}]{_GROUP})+(?![\w]|[.,/-]\w)")

Span = Tuple[int, int, Tuple[str, ...]]   # (start, end, the span's forms)


def _amount(raw: str) -> List[str]:
    """The canonical amount first, then its integer part (a fragment) when that is long enough."""
    compact = re.sub(f"[{_SPACE}']", "", raw)
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


def _date_spans(text: str) -> Iterable[Span]:
    for m in _YMD.finditer(text):
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            yield m.start(), m.end(), (iso,)
    for m in _DMY.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        readings = tuple(sorted(iso for iso in {_iso(y, b, a), _iso(y, a, b)} if iso))   # day- and month-first
        if readings:
            yield m.start(), m.end(), readings


def _amount_spans(text: str) -> Iterable[Span]:
    for m in _AMOUNT.finditer(text):
        forms = _amount(m.group(1))
        if forms:
            yield m.start(1), m.end(1), tuple(forms)


def _group_spans(text: str) -> Iterable[Span]:
    for m in _GROUPS.finditer(text):
        joined = re.sub(f"[{_SPACE}+]", "", m.group(0)).casefold()
        if sum(ch.isdigit() for ch in joined) >= _GROUP_MIN_DIGITS:
            yield m.start(), m.end(), (joined,)


def _id_forms(token: str) -> Tuple[str, List[str]]:
    """(the token's maximal form or "", its fragments). The maximal form is the token with its separators stripped,
    casefolded. Digits are joined only when no `.` or `,` sits between them: a decimal, a dotted date or a version
    is not one number."""
    runs = _DIGITS.findall(token)
    joined = re.sub(r"[-/_.]", "", token).casefold()
    dotted = "." in token or "," in token
    fragments = [run for run in runs if len(run) >= _MIN_DIGITS]
    digits = "".join(runs)
    if not dotted and len(digits) >= _MIN_DIGITS and digits != joined:
        fragments.append(digits)
    if len(joined) < _MIN_DIGITS or (dotted and joined.isdigit()):
        return "", fragments
    return joined, fragments


def _id_spans(text: str) -> Iterable[Tuple[int, int, str, List[str]]]:
    for m in _ID.finditer(text):
        token = m.group(0)
        if any(ch.isdigit() for ch in token):
            maximal, fragments = _id_forms(token)
            yield m.start(), m.end(), maximal, fragments


def _terms(text: str) -> Set[str]:
    """The index side: every form of every span, maximal and fragment."""
    terms: Set[str] = set()
    for _, _, forms in (*_date_spans(text), *_amount_spans(text), *_group_spans(text)):
        terms.update(forms)
    for _, _, maximal, fragments in _id_spans(text):
        if maximal:
            terms.add(maximal)
        terms.update(fragments)
    return terms


def _maximal(text: str) -> Set[str]:
    """The query side: each span's maximal form only, the first kind that claims a stretch of text winning — dates,
    then amounts, then digit groups, then ids. A part of a span (a date's year, an amount's integer part, a run inside
    an id) is a fragment: as a query it would match documents that share only that part."""
    taken: List[Tuple[int, int]] = []
    terms: Set[str] = set()

    def claim(start: int, end: int, forms: Iterable[str]) -> None:
        if any(start < e and s < end for s, e in taken):
            return
        taken.append((start, end))
        terms.update(f for f in forms if f)

    for start, end, forms in _date_spans(text):
        claim(start, end, forms)
    for start, end, forms in _amount_spans(text):
        claim(start, end, forms[:1])
    for start, end, forms in _group_spans(text):
        claim(start, end, forms)
    for start, end, maximal, _ in _id_spans(text):
        claim(start, end, (maximal,))
    return terms


def content_terms(text: str) -> Tuple[str, ...]:
    """A chunk's terms. Bounded: past the cap, the longest (most specific) terms are kept."""
    terms = _terms(text)
    if len(terms) > CONTENT_TERMS_PER_CHUNK:
        terms = set(sorted(terms, key=lambda t: (-len(t), t))[:CONTENT_TERMS_PER_CHUNK])
    return tuple(sorted(terms))


def query_terms(query: str) -> Tuple[str, ...]:
    """The query's maximal forms, most specific (longest) first; the search queries at most CONTENT_TERM_QUERIES of
    them. Each is also a form `content_terms` stores for the same text."""
    return tuple(sorted(_maximal(query), key=lambda t: (-len(t), t)))
