"""
Content exact-token stream keys (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.6). Pure; one normaliser for indexing
and for the query, so `1 250,00 €` in a scan and `1250.00` in a question meet. A change bumps EXTRACTION_VERSION (Search v2: KEYS_VERSION, §25.4.8).

Public interface:
    content_terms(text) -> Tuple[str, ...]   sorted, unique, at most CONTENT_TERMS_PER_CHUNK
    query_terms(query) -> Tuple[str, ...]    the query's maximal forms, most specific first
    clip_query(query, max_chars) -> str      at most max_chars of the query, never ending inside a token
    term_spans(text) / query_term_spans(query)   the same, with each span's position and category (Search v2 keyer)

Terms (the index side keeps every form; the query only the maximal form of each span, review D-M1 — and the integer of
an amount with zero cents, the same value, arbiter R2 S-m2):
    amounts   "1 250,00" "1.250,00" "1,250.00" "1250.5"  -> "1250.00" / "1250.50"; index also the integer part "1250"
    dates     "15.03.2024" "03/15/2024" "2024-03-15"     -> "2024-03-15" (both readings when day and month are <= 12)
              "15 March 2024" "March 15, 2024" "15 березня 2024" "15 марта 2024" "15 de marzo de 2024" "15 mars 2024"
                                                          -> "2024-03-15" (a day, a month name in en/es/fr/uk/ru with its
                                                          case forms, a year; a month and year alone give no date term)
    ids       "INV-2024/0012"                             -> "inv20240012"; index also "20240012", "2024", "0012"
    groups    "XX00 1234 5678 …" "+34 600 000 000"        -> "xx0012345678…", "34600000000" (_GROUP_MIN_DIGITS on)
A token holding `.` or `,` never yields its digits joined, on either side: "1250.00" is not "125000".
"""
from __future__ import annotations

import re
from bisect import bisect_left
from datetime import date
from typing import Dict, Iterable, List, Set, Tuple

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

# Month names and their case forms (final review F5-5; locatives "в марте", "у березні": Search v2 P1 review m2), matched case-insensitively; each maps to its month number.
_MONTH_FORMS: Dict[int, Tuple[str, ...]] = {
    1: ("january", "jan", "enero", "janvier", "січня", "січень", "января", "январь", "янв", "январе", "січні"),
    2: ("february", "feb", "febrero", "février", "fevrier", "févr", "fevr", "лютого", "лютий", "февраля", "февраль",
        "фев", "феврале", "лютому"),
    3: ("march", "mar", "marzo", "mars", "березня", "березень", "марта", "март", "марте", "березні"),
    4: ("april", "apr", "abril", "avril", "квітня", "квітень", "апреля", "апрель", "апр", "апреле", "квітні"),
    5: ("may", "mayo", "mai", "травня", "травень", "мая", "май", "мае", "травні"),
    6: ("june", "jun", "junio", "juin", "червня", "червень", "июня", "июнь", "июне", "червні"),
    7: ("july", "jul", "julio", "juillet", "липня", "липень", "июля", "июль", "июле", "липні"),
    8: ("august", "aug", "agosto", "août", "aout", "серпня", "серпень", "августа", "август", "авг", "августе",
        "серпні"),
    9: ("september", "sep", "sept", "septiembre", "setiembre", "septembre", "вересня", "вересень", "сентября",
        "сентябрь", "сен", "сент", "сентябре", "вересні"),
    10: ("october", "oct", "octubre", "octobre", "жовтня", "жовтень", "октября", "октябрь", "окт", "октябре",
        "жовтні"),
    11: ("november", "nov", "noviembre", "novembre", "листопада", "листопад", "ноября", "ноябрь", "ноя", "ноябре",
        "листопаді"),
    12: ("december", "dec", "diciembre", "décembre", "decembre", "déc", "грудня", "грудень", "декабря", "декабрь",
        "дек", "декабре", "грудні"),
}
_MONTH_OF: Dict[str, int] = {form: month for month, forms in _MONTH_FORMS.items() for form in forms}
_MONTH = "(" + "|".join(re.escape(f) for f in sorted(_MONTH_OF, key=lambda f: (-len(f), f))) + r")\.?(?![\w])"
_ORDINAL = r"(?:st|nd|rd|th|er|e)?"
# "1 March 2026", "1st of March 2026", "1 de marzo de 2026", "1er mars 2026", "1 березня 2026 року"
_DAY_MONTH_YEAR = re.compile(rf"(?<![\w.,/-])(\d{{1,2}}){_ORDINAL}\s+(?:(?:de|of)\s+)?{_MONTH},?\s+(?:(?:de|del)\s+)?"
                             rf"(\d{{4}})(?!\d)", re.IGNORECASE)
# "March 1, 2026", "Mar 1st 2026"
_MONTH_DAY_YEAR = re.compile(rf"(?<![\w]){_MONTH}\s+(\d{{1,2}}){_ORDINAL},?\s+(\d{{4}})(?!\d)", re.IGNORECASE)

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
    for m in _DAY_MONTH_YEAR.finditer(text):
        iso = _iso(int(m.group(3)), _MONTH_OF[m.group(2).casefold()], int(m.group(1)))
        if iso:
            yield m.start(), m.end(), (iso,)
    for m in _MONTH_DAY_YEAR.finditer(text):
        iso = _iso(int(m.group(3)), _MONTH_OF[m.group(1).casefold()], int(m.group(2)))
        if iso:
            yield m.start(), m.end(), (iso,)


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


TermSpan = Tuple[int, int, Tuple[str, ...], str]   # (start, end, forms, category: date | amount | group | id)


def term_spans(text: str) -> Tuple[TermSpan, ...]:
    """The index side with positions (Search v2 keyer, §25.2.4): every span with every form, maximal and fragment,
    in text order. Spans may overlap; `content_terms` is the union of their forms (before its cap)."""
    spans: List[TermSpan] = []
    spans.extend((s, e, forms, "date") for s, e, forms in _date_spans(text))
    spans.extend((s, e, forms, "amount") for s, e, forms in _amount_spans(text))
    spans.extend((s, e, forms, "group") for s, e, forms in _group_spans(text))
    for start, end, maximal, fragments in _id_spans(text):
        forms = ((maximal,) if maximal else ()) + tuple(fragments)
        if forms:
            spans.append((start, end, forms, "id"))
    return tuple(sorted(spans, key=lambda span: (span[0], span[1])))


def _terms(text: str) -> Set[str]:
    """The index side: every form of every span, maximal and fragment."""
    return {form for span in term_spans(text) for form in span[2]}


def query_term_spans(query: str) -> Tuple[TermSpan, ...]:
    """The query side with positions: each claimed stretch with its maximal forms (possibly none: an id too short for
    a form still claims its stretch), the first kind that claims a stretch of text winning — dates, then amounts,
    then digit groups, then ids. A part of a span (a date's year, an amount's integer part, a run inside an id) is a
    fragment: as a query it would match documents that share only that part — except a whole amount's integer,
    which is the same value. In text order."""
    # The claimed stretches never overlap, so sorted by start they are sorted by end too: the only one that can
    # overlap [start, end) is the last one starting before `end` (bisect, not a scan of every claim: arbiter R2 S-m1).
    starts: List[int] = []
    ends: List[int] = []
    claimed: List[TermSpan] = []

    def claim(start: int, end: int, forms: Iterable[str], category: str) -> None:
        i = bisect_left(starts, end)
        if i > 0 and ends[i - 1] > start:
            return
        starts.insert(i, start)
        ends.insert(i, end)
        claimed.insert(i, (start, end, tuple(f for f in forms if f), category))

    for start, end, forms in _date_spans(query):
        claim(start, end, forms, "date")
    for start, end, forms in _amount_spans(query):
        # zero cents are the integer's value: "1,250.00" also queries "1250" (arbiter R2 S-m2), the integer part
        # the index side stores for the same text; any other cents are another value and query their form only
        claim(start, end, forms if forms[0].endswith(".00") else forms[:1], "amount")
    for start, end, forms in _group_spans(query):
        claim(start, end, forms, "group")
    for start, end, maximal, _ in _id_spans(query):
        claim(start, end, (maximal,), "id")
    return tuple(claimed)


def _maximal(text: str) -> Set[str]:
    """The query side: each claimed span's maximal forms (`query_term_spans`)."""
    return {form for span in query_term_spans(text) for form in span[2]}


def content_terms(text: str) -> Tuple[str, ...]:
    """A chunk's terms. Bounded: past the cap, the longest (most specific) terms are kept."""
    return capped_terms(_terms(text))


def capped_terms(terms: Set[str]) -> Tuple[str, ...]:
    """`content_terms`' cap over a term set already extracted (`term_spans`): the longest terms kept."""
    if len(terms) > CONTENT_TERMS_PER_CHUNK:
        terms = set(sorted(terms, key=lambda t: (-len(t), t))[:CONTENT_TERMS_PER_CHUNK])
    return tuple(sorted(terms))


def query_terms(query: str) -> Tuple[str, ...]:
    """The query's maximal forms, most specific (longest) first; the search queries at most CONTENT_TERM_QUERIES of
    them. Each is also a form `content_terms` stores for the same text."""
    return tuple(sorted(_maximal(query), key=lambda t: (-len(t), t)))


def clip_query(query: str, max_chars: int) -> str:
    """At most `max_chars` characters of `query` for `query_terms` (arbiter R2 S-m1), cut at the last whitespace so no
    token is cut in half (a halved id would query a fragment). One token longer than the cap keeps its prefix."""
    if len(query) <= max_chars:
        return query
    head = query[:max_chars]
    if query[max_chars].isspace():
        return head.rstrip()
    cut = max(head.rfind(ch) for ch in (" ", "\t", "\n", *_SPACE))
    return head[:cut].rstrip() if cut > 0 else head
