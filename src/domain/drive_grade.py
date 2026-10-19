"""
Drive grade — what the stored text of a file holds, as the tests of evidence grading
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §26.4, D35, Δ59). Pure: no I/O, no clock.

`TextEvidence` is one file's stored text (its chunks' texts) and name, with the facts the grading tests read: the
normalised text, its number terms (`drive_content_terms`), and, computed once and only when a test needs them, the dates
the text prints in any format, the text with its separators removed, its `w:` word keys and its transliteration
skeletons. `grade_profile` and `grade_brief` (`drive_profile`) ask it per value; each answer is a `Grade` and the
text's own form of the value (`seen_as`).

Public interface:
    TextEvidence(texts, name)
        .occurs(printed, *, in_name=False) -> bool
        .date_grade(iso, printed) -> (Grade, seen_as)
        .prints_date(day) -> bool                 the text prints the date in a format the lenient reader knows
        .value_grade(printed, canonical) -> (Grade, canonical, seen_as)
        .term_grade(term) -> (Grade, seen_as)
        .skeleton_seen(spelling) -> str           the text's words that share the spelling's skeletons, or ""
        .words_grade(phrase) -> Grade             a native topic against the text's word keys
    lenient_date(printed) -> Optional[date]       any separators incl. spaces, day-month-year first, month names (bilingual
                                                  pairs, abbreviations, compact "13JUN2024") and ISO basic ("20261009T180724Z")
    norm(text) -> str                             NFC + casefold + whitespace collapse (the matching form)
    normalise_value(printed) -> str               the casefolded letters and digits of a value, accents kept

The lenient date reads ONE date: day-month-year (a leading four-digit group reads year-month-day); a month-first
reading only when the day-first one is not a date ("05/31/2031"). A printed form that reads as a different date than
the profile's ISO is refused, never rescued by its other reading: the number-term test (`in_text`) already accepts
both readings of a form the text prints itself.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from .drive_content_terms import _MONTH_OF, amount_or_date as _amount_or_date, content_terms, query_terms
from .drive_enrichment import Grade
from .drive_key_forms import skeleton_key, token_forms, token_spans
from .drive_keys import word_form_keys
from .drive_name_keys import _is_cyrillic

_STRIPPED_MIN = 3                      # a separator-free match needs this many letters and digits
_NUMERIC_DATE = re.compile(r"\d+")
# Month abbreviations the passport and ISO-compact forms print ("ЛИП/JUL", "13JUN2024"), per language; the content-term
# table keeps its own (full names and a few abbreviations): this one is the grader's, merged under it, never over it.
_ABBREVIATIONS: Dict[int, Tuple[str, ...]] = {
    1: ("jan", "ene", "janv", "січ", "янв"),
    2: ("feb", "févr", "февр", "лют", "фев"),
    3: ("mar", "мар", "бер"),
    4: ("apr", "abr", "avr", "кві", "квіт", "апр"),
    5: ("may", "mai", "тра", "трав", "май"),
    6: ("jun", "juin", "чер", "черв", "июн"),
    7: ("jul", "juil", "лип", "июл"),
    8: ("aug", "ago", "août", "сер", "серп", "авг"),
    9: ("sep", "sept", "set", "вер", "сен", "сент"),
    10: ("oct", "жов", "жовт", "окт"),
    11: ("nov", "лис", "лист", "ноя", "нояб"),
    12: ("dec", "dic", "déc", "гру", "груд", "дек"),
}
_GRADE_MONTH_OF: Dict[str, int] = dict(_MONTH_OF)
for _month, _forms in _ABBREVIATIONS.items():
    for _form in _forms:
        _GRADE_MONTH_OF.setdefault(_form, _month)
_MONTH_ALTS = "|".join(re.escape(f) for f in sorted(_GRADE_MONTH_OF, key=lambda f: (-len(f), f)))
_MON_END = r"\.?(?![^\W\d])"          # not followed by a letter: a digit may follow ("13JUN2024")
# a month name, or a bilingual pair as passports print it ("MAY/MAI", "ЛИП/JUL"): both must read as the same month
_MONTH = rf"(?P<m1>{_MONTH_ALTS}){_MON_END}(?:\s*/\s*(?P<m2>{_MONTH_ALTS}){_MON_END})?"
_ORD = r"(?:st|nd|rd|th|er|e)?"
_NAMED_DAY_FIRST = re.compile(
    rf"(?<!\d)(?P<d>\d{{1,2}}){_ORD}[\W_]*(?:(?:de|of)[\W_]+)?{_MONTH}[\W_]*(?:(?:de|del)[\W_]+)?(?P<y>\d{{4}}|\d{{2}})(?!\d)",
    re.IGNORECASE)
_NAMED_MONTH_FIRST = re.compile(rf"(?<![\w]){_MONTH}[\W_]+(?P<d>\d{{1,2}}){_ORD}[\W_]+(?P<y>\d{{4}})(?!\d)", re.IGNORECASE)
# a day and a month with no year ("5 жовтня", "5 de octubre", "October 5"): graded only with the year found elsewhere
_DAY_MONTH_ONLY = re.compile(rf"\s*(?P<d>\d{{1,2}}){_ORD}[\W_]*(?:(?:de|of)[\W_]+)?{_MONTH}\s*", re.IGNORECASE)
_MONTH_DAY_ONLY = re.compile(rf"\s*{_MONTH}[\W_]+(?P<d>\d{{1,2}}){_ORD}\s*", re.IGNORECASE)
_ISO_BASIC = re.compile(r"\s*(\d{4})(\d{2})(\d{2})(?:T\d{6}Z?)?\s*", re.IGNORECASE)   # 20261009, 20261009T180724Z


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def normalise_value(printed: str) -> str:
    """The casefolded letters and digits of a value, nothing else."""
    return "".join(ch for ch in unicodedata.normalize("NFC", printed).casefold() if ch.isalnum())


def _date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month_of(m: "re.Match[str]") -> Optional[int]:
    """The month a (possibly bilingual) month match reads as; None when the two names disagree."""
    first = _GRADE_MONTH_OF[m.group("m1").casefold()]
    second = m.group("m2")
    if second is not None and _GRADE_MONTH_OF[second.casefold()] != first:
        return None
    return first


def day_month_of(printed: str) -> Optional[Tuple[int, int]]:
    """(month, day) of a printed form that is a day and a month name and nothing else ("5 жовтня"); None otherwise."""
    text = unicodedata.normalize("NFC", printed)
    m = _DAY_MONTH_ONLY.fullmatch(text) or _MONTH_DAY_ONLY.fullmatch(text)
    if m is None:
        return None
    month = _month_of(m)
    return None if month is None else (month, int(m.group("d")))


def lenient_date(printed: str, expected: Optional[date] = None) -> Optional[date]:
    """The one date a printed form reads as (module docstring), or None when it is not a date. A two-digit year is read
    only against `expected` (the profile's ISO date): it is that year when it equals the last two digits of the
    expected year, no century pivot ("07.05.31" against 2031-05-07); without `expected` it is no date."""
    text = unicodedata.normalize("NFC", printed)
    named = _NAMED_DAY_FIRST.search(text)
    if named is not None:
        month = _month_of(named)
        year = named.group("y")
        if month is None:
            return None
        if len(year) == 2:
            if expected is None or expected.year % 100 != int(year):
                return None
            return _date(expected.year, month, int(named.group("d")))
        return _date(int(year), month, int(named.group("d")))
    named = _NAMED_MONTH_FIRST.search(text)
    if named is not None:
        month = _month_of(named)
        return None if month is None else _date(int(named.group("y")), month, int(named.group("d")))
    basic = _ISO_BASIC.fullmatch(text)
    if basic is not None:
        return _date(int(basic.group(1)), int(basic.group(2)), int(basic.group(3)))
    groups = _NUMERIC_DATE.findall(text)
    if len(groups) != 3:
        return None
    a, b, c = groups
    if len(a) == 4 and len(b) <= 2 and len(c) <= 2:
        return _date(int(a), int(b), int(c))
    if len(c) == 4 and len(a) <= 2 and len(b) <= 2:
        return _date(int(c), int(b), int(a)) or _date(int(c), int(a), int(b))
    if len(c) == 2 and len(a) <= 2 and len(b) <= 2 and expected is not None and expected.year % 100 == int(c):
        return _date(expected.year, int(b), int(a)) or _date(expected.year, int(a), int(b))
    return None


class TextEvidence:
    """One file's stored text and name, and the evidence tests over them (module docstring)."""

    def __init__(self, texts: Sequence[str], name: str = "") -> None:
        self._raw = "\n".join(texts)
        self.haystack = norm(self._raw)
        self.name_norm = norm(name)
        terms: Set[str] = set()
        for text in texts:
            terms.update(content_terms(text))
        self.terms: FrozenSet[str] = frozenset(terms)
        self._dates: Optional[FrozenSet[date]] = None
        self._stripped: Optional[Tuple[str, List[int]]] = None
        self._word_keys: Optional[FrozenSet[str]] = None
        self._skeletons: Optional[Dict[Tuple[str, bool], str]] = None

    # -- facts, computed on first use ---------------------------------------------------------------------------

    def _dates_in_text(self) -> FrozenSet[date]:
        """Every date the text prints in a format the lenient reader knows: the day-month-year groups with any
        separators and the month-name forms (the strict normaliser's dates are in `terms`)."""
        if self._dates is None:
            found: Set[date] = set()
            for m in re.finditer(r"(?<!\d)\d{1,4}(?:[^\w\n]{1,3}\d{1,4}){2}(?!\d)", self._raw):
                read = lenient_date(m.group(0))
                if read is not None:
                    found.add(read)
            for pattern in (_NAMED_DAY_FIRST, _NAMED_MONTH_FIRST):
                for m in pattern.finditer(self._raw):
                    read = lenient_date(m.group(0))
                    if read is not None:
                        found.add(read)
            self._dates = frozenset(found)
        return self._dates

    def _stripped_text(self) -> Tuple[str, List[int]]:
        """The text's casefolded letters and digits, and for each the index of its character in the raw text."""
        if self._stripped is None:
            chars: List[str] = []
            where: List[int] = []
            for i, ch in enumerate(unicodedata.normalize("NFC", self._raw)):
                if ch.isalnum():
                    for folded in ch.casefold():
                        chars.append(folded)
                        where.append(i)
            self._stripped = ("".join(chars), where)
        return self._stripped

    def _words(self) -> FrozenSet[str]:
        if self._word_keys is None:
            self._word_keys = frozenset(word_form_keys(self._raw))
        return self._word_keys

    def _skeleton_words(self) -> Dict[Tuple[str, bool], str]:
        """The text's words by (skeleton, is Cyrillic): a skeleton match counts across scripts only."""
        if self._skeletons is None:
            out: Dict[Tuple[str, bool], str] = {}
            for token, start, end in token_spans(self._raw):
                key = skeleton_key(token)
                if key and (key, _is_cyrillic(token)) not in out:
                    out[(key, _is_cyrillic(token))] = self._raw[start:end].strip() or token
            self._skeletons = out
        return self._skeletons

    # -- tests ----------------------------------------------------------------------------------------------------

    def occurs(self, printed: str, *, in_name: bool = False) -> bool:
        """`printed` occurs in the text (or, with `in_name`, in the file's name too), on word boundaries."""
        needle = norm(printed)
        if not needle:
            return False
        pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
        return (pattern.search(self.haystack) is not None
                or (in_name and pattern.search(self.name_norm) is not None))

    def _separator_free(self, printed: str) -> str:
        """The text's own spelling of `printed` once separators are ignored ("X1234567A" for "X 1234567 A"), whole
        alphanumeric runs only (it must not start or end inside a word); "" when the text has none."""
        needle = normalise_value(printed)
        if len(needle) < _STRIPPED_MIN or _amount_or_date(printed):
            return ""
        stripped, where = self._stripped_text()
        raw = unicodedata.normalize("NFC", self._raw)
        end_of = len(needle)
        start = stripped.find(needle)
        while start != -1:
            first, last = where[start], where[start + end_of - 1]
            starts_run = (first == 0 or not raw[first - 1].isalnum()) and (start == 0 or where[start - 1] != first)
            after = start + end_of
            ends_run = (last + 1 >= len(raw) or not raw[last + 1].isalnum()) and (
                after >= len(stripped) or where[after] != last)
            if starts_run and ends_run:
                return raw[first:last + 1]
            start = stripped.find(needle, start + 1)
        return ""

    def prints_date(self, day: date) -> bool:
        """The text prints this date in a format the lenient reader knows (a brief's date has no `printed` form)."""
        return day in self._dates_in_text()

    def _year_elsewhere(self, year: int) -> bool:
        """The year occurs in the text as a number term (a year printed apart from a day and month)."""
        return str(year) in self.terms

    def date_grade(self, iso: date, printed: str) -> Tuple[Grade, str]:
        """A date (§26.4). `in_text`: its ISO form is among the text's number terms. `in_text_as`: `printed` occurs
        and its lenient reading is this date; else `not_in_text`."""
        if iso.isoformat() in self.terms:
            return Grade.IN_TEXT, ""
        if printed.strip() and self.occurs(printed):
            if lenient_date(printed, iso) == iso:
                return Grade.IN_TEXT_AS, printed.strip()
            if day_month_of(printed) == (iso.month, iso.day) and self._year_elsewhere(iso.year):
                return Grade.IN_TEXT_AS, printed.strip()     # D37: "5 жовтня", the year printed apart from the day
        return Grade.NOT_IN_TEXT, ""

    def value_grade(self, printed: str, canonical: str) -> Tuple[Grade, str, str]:
        """A key value (§26.4): (grade, the canonical form to keep, seen_as). `in_text`: the maximal number term of
        `printed`, else of `canonical`, is the text's (never a fragment of it), or `printed` occurs verbatim (the
        canonical is then its first number term, else its casefolded spelling without spaces). `in_text_as`: the
        value's letters and digits, casefolded, occur in the text with separators removed (canonical = those)."""
        candidates = [terms_[0] for terms_ in (query_terms(printed), query_terms(canonical)) if terms_]
        hit = next((t for t in candidates if t in self.terms), None)
        if hit is not None:
            return Grade.IN_TEXT, hit, ""
        if printed.strip() and self.occurs(printed):
            printed_terms = query_terms(printed)
            return Grade.IN_TEXT, printed_terms[0] if printed_terms else "".join(norm(printed).split()), ""
        seen = self._separator_free(printed) or self._separator_free(canonical)
        if seen:
            return Grade.IN_TEXT_AS, normalise_value(seen), seen
        return Grade.NOT_IN_TEXT, canonical, ""

    def term_grade(self, term: str) -> Tuple[Grade, str]:
        """A number term of the profile's prose, graded like a date or a value (§26.4)."""
        if term in self.terms:
            return Grade.IN_TEXT, ""
        read = _iso_of(term)
        if read is not None:
            return (Grade.IN_TEXT_AS, term) if read in self._dates_in_text() else (Grade.NOT_IN_TEXT, "")
        seen = self._separator_free(term)
        return (Grade.IN_TEXT_AS, seen) if seen else (Grade.NOT_IN_TEXT, "")

    def skeleton_seen(self, spelling: str) -> str:
        """The text's words whose transliteration skeletons are the spelling's, in order ("Барселона" for
        "Barcelona"); "" unless every word of the spelling has one. Across scripts only: two Latin spellings
        ("Maria" and "Mario" share a Cyrillic stem) are not a transliteration (xc F3)."""
        found: List[str] = []
        by_key = self._skeleton_words()
        for token, _, _ in token_spans(spelling):
            key = skeleton_key(token)
            word = by_key.get((key, not _is_cyrillic(token))) if key else None   # the other script's word
            if word is None:
                return ""
            found.append(word)
        return " ".join(found)

    def words_grade(self, phrase: str) -> Grade:
        """A native topic (§26.4), by its words' native forms among the text's word keys: `in_text` when the phrase
        occurs as written, `in_text_as` when every word is there in some form, else `not_in_text`."""
        if self.occurs(phrase):
            return Grade.IN_TEXT
        tokens = [t for t, _, _ in token_spans(phrase)]
        words = self._words()
        if tokens and all(any(f"w:{form}" in words for form in token_forms(t)) for t in tokens):
            return Grade.IN_TEXT_AS
        return Grade.NOT_IN_TEXT


def _iso_of(term: str) -> Optional[date]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", term):
        try:
            return date.fromisoformat(term)
        except ValueError:
            return None
    return None


def prose_terms(*texts: str) -> Tuple[str, ...]:
    """The number terms of the profile's prose (title, what_it_is), sorted and unique: the maximal form of each span
    only (`query_terms`), never the fragments a text index also keeps (the "2024" and "0012" of "INV-2024/0012")."""
    seen: Set[str] = set()
    for text in texts:
        seen.update(query_terms(text))
    return tuple(sorted(seen))
