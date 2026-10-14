"""
Drive key forms — tokens, per-token form sets, transliteration and the skeleton of the Search v2 keyer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4). Pure: stdlib only. `drive_keys` builds every key from these.

Public interface:
    scan(text) -> (Token, ...)               tokens with spans, case parts and hyphen compounds (index and query)
    token_spans(text) -> ((token, start, end), ...)
    token_forms(token) -> (form, ...)        native forms: Cyrillic (a)-(e), Latin (a)-(b), a 2-letter word exactly,
                                             a digit token under 3 digits as itself
    skeleton_key(token) -> str | None        the ONE transliteration skeleton the index stores per word (`x:`)
    query_skeletons(token) -> (str, ...)     every skeleton an index word of this spelling can carry (query side:
                                             both Cyrillic schemes, alternations both ways)
    primary(token) -> str                    form (b) in the token's own script (phrase parts)
    is_short_function_word(token), SHORT_FUNCTION_WORDS, FORM_TABLES (hashed by drive_keys)

The index stores only native forms plus one skeleton per word (review B2); the query generates the variants. A match
through a skeleton crosses scripts or spellings: it is a translation, never exact (review M8, §25.5.4).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import FrozenSet, List, Mapping, NamedTuple, Optional, Tuple

from .drive_name_keys import _BOUNDARY, _LAT_TO_CYR, _LATIN_FOLDS, _QUOTE_LIKE, _is_cyrillic, _strip_accents

MIN_FORM_LETTERS = 3
SHORT_WORD_LETTERS = 2          # a word of 2 letters is keyed exactly (form (a) only), unless a function word
ALNUM_RUN_MAX = 6               # a letter-digit run this short also keeps its joined form ("X5", "A4", "ред3")
NUMBER_MIN_DIGITS = 3           # = drive_content_terms._MIN_DIGITS: longer digit tokens are number terms (`n:`)
_CYR_ENDINGS: Tuple[str, ...] = tuple(sorted((
    "ами ями ого его ому ему ой ей ий ый ым им ых их ая яя ое ее ую юю ою ею ые ие ыми ими ові еві ої ій ім іх ими "
    "іми ів ом ем ах ях ам ям ов ев ы и а я у ю е є і ї о").split(), key=lambda e: (-len(e), e)))
_SOFT_FINALS: Tuple[str, ...] = ("й", "ь")
_FLEETING: Tuple[Tuple[str, str], ...] = (("ень", "нь"), ("ок", "к"), ("ек", "к"), ("ец", "ц"))
_ALTERNATIONS: Mapping[str, Tuple[str, ...]] = {"і": ("о", "е"), "ї": ("є",)}
_REVERSE_ALTERNATIONS: Mapping[str, str] = {"о": "і", "е": "і", "є": "ї"}     # query skeletons only
_ALTERNATION_MIN_LETTERS = 4
_CYR_VOWELS = frozenset("аеиіїоуюяєыэё")
_C_SOFT_BEFORE = ("e", "i", "y")     # Latin `c` before these is с, elsewhere к (general Latin orthography)
# Ukrainian national transliteration in reverse (Latin -> Ukrainian): word-initial ye yi yu ya, inside ie iu ia.
_UK_FROM_LAT: Tuple[Tuple[str, str], ...] = tuple(sorted([
    ("shch", "щ"), ("zgh", "зг"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("ie", "є"),
    ("iu", "ю"), ("ia", "я"), ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "ґ"), ("h", "г"),
    ("i", "і"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"),
    ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "и"), ("z", "з"),
], key=lambda pair: (-len(pair[0]), pair[0])))
_UK_FROM_LAT_INITIAL: Tuple[Tuple[str, str], ...] = (("ye", "є"), ("yi", "ї"), ("yu", "ю"), ("ya", "я"))
# The skeleton: Cyrillic letters by sound class, so both Russian and Ukrainian spellings and both schemes meet.
_SKELETON: Mapping[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "h", "д": "d", "е": "e", "ё": "e", "є": "e", "ж": "zh", "з": "z",
    "и": "i", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i",
    "ь": "", "э": "e", "ю": "u", "я": "a", "ʼ": "",
}
_APOSTROPHES = frozenset("'’ʼ`´ʹʻʽ‘")     # inside a Cyrillic word: part of the word (Ukrainian apostrophe)
APOSTROPHE = "ʼ"                           # the one code point a word keeps (U+02BC)
_SOFT_HYPHEN = "­"
_HYPHENS = frozenset("-‐‑")
# 1-2 letter function words (en, ru, uk, es, fr): not keyed on the index, ignored in a query (review M2).
SHORT_FUNCTION_WORDS: FrozenSet[str] = frozenset("""
a i an as at be by do he if in is it me my no of on or so to up us we
а в во да до же за и из к ко ли на не ни но о об от по с со то у я мы вы он бы их
з із зі і й та це як ми ви є
al de el en es la le lo mi se si su te tu un y e u o
à au ce du et il je ma ne on ou sa ta l d s n c j m t qu
""".split())
FORM_TABLES = (ALNUM_RUN_MAX, MIN_FORM_LETTERS, SHORT_WORD_LETTERS, NUMBER_MIN_DIGITS, _CYR_ENDINGS, _SOFT_FINALS,
               _FLEETING,
               sorted(_ALTERNATIONS.items()), sorted(_REVERSE_ALTERNATIONS.items()), _ALTERNATION_MIN_LETTERS,
               sorted(_CYR_VOWELS), _C_SOFT_BEFORE, _UK_FROM_LAT, _UK_FROM_LAT_INITIAL, sorted(_SKELETON.items()),
               sorted(_APOSTROPHES), APOSTROPHE, _SOFT_HYPHEN, sorted(_HYPHENS), sorted(SHORT_FUNCTION_WORDS), _LAT_TO_CYR,
               sorted(_LATIN_FOLDS.items()), sorted(_QUOTE_LIKE), _BOUNDARY.pattern, "digit-boundary, word-run v2")

_CASE_SPLIT = re.compile(r"(?<=[a-zа-яёіїєґ])(?=[A-ZА-ЯЁІЇЄҐ])")


class Token(NamedTuple):
    """One token: NFC, casefold, Cyrillic stress marks dropped, ё folded to е, a Ukrainian apostrophe and a soft
    hyphen removed. `parts`: its camelCase parts (index side, "iPhone" -> "i", "phone"). `compound`: the joined word
    of the hyphen compound ("Wi-Fi" -> "wifi") or of the short letter-digit run ("X5" -> "x5") this token starts,
    `compound_end` its end, `compound_kind` "hyphen" or "alnum"."""
    text: str
    start: int
    end: int
    parts: Tuple[str, ...] = ()
    compound: str = ""
    compound_end: int = -1
    compound_kind: str = ""


_DIGIT_BOUNDARY = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")
_COMBINING_CYR = re.compile("[\u0300-\u036f\u0483-\u0489]")
_CYRILLIC = re.compile("[а-яёіїєґ]")
_LINEBREAK_GAP = re.compile(r"[-\u2010\u2011][^\S\n]*\n\s*")
_CYR_LETTER = re.compile("[а-яёіїєґА-ЯЁІЇЄҐ]")


@lru_cache(maxsize=1)
def _word_run() -> re.Pattern[str]:
    """A run of word characters: letters and digits (not `_`, not the apostrophe look-alikes of
    `drive_name_keys._QUOTE_LIKE`), combining marks, and the soft hyphen (dropped later). Everything else separates: Unicode
    punctuation, symbols, separators and controls."""
    marks = "".join(re.escape(chr(c)) for c in range(0x300, 0x10000) if unicodedata.category(chr(c))[0] == "M")
    quotes = "".join(re.escape(q) for q in sorted(_QUOTE_LIKE))
    return re.compile(rf"(?:[^\W_{quotes}]|[{marks}\u00ad])+")


def _runs(text: str) -> List[Tuple[str, List[int]]]:
    """Word runs as (characters, original index of each): a soft hyphen is dropped; an apostrophe between two
    Cyrillic letters joins (kept as APOSTROPHE), and so does a line-break hyphen between lowercase letters."""
    runs: List[Tuple[str, List[int]]] = []
    prev_end = -1
    for m in _word_run().finditer(text):
        raw, start = m.group(), m.start()
        idx = list(range(start, m.end()))
        if _SOFT_HYPHEN in raw:
            keep = [k for k, ch in enumerate(raw) if ch != _SOFT_HYPHEN]
            raw, idx = "".join(raw[k] for k in keep), [idx[k] for k in keep]
            if not raw:
                continue
        if runs and prev_end >= 0:
            gap = text[prev_end:start]
            last = runs[-1][0][-1]
            if len(gap) == 1 and gap in _APOSTROPHES and _CYR_LETTER.match(last) and _CYR_LETTER.match(raw[0]):
                runs[-1] = (runs[-1][0] + APOSTROPHE + raw, runs[-1][1] + [prev_end] + idx)
                prev_end = m.end()
                continue
            if last.islower() and raw[0].islower() and _LINEBREAK_GAP.fullmatch(gap):
                runs[-1] = (runs[-1][0] + raw, runs[-1][1] + idx)
                prev_end = m.end()
                continue
        runs.append((raw, idx))
        prev_end = m.end()
    return runs


def _normalise(raw: str) -> str:
    token = unicodedata.normalize("NFC", raw).casefold()
    if _CYRILLIC.search(token):
        token = _COMBINING_CYR.sub("", token).replace("ё", "е")
    return token


def scan(text: str) -> Tuple[Token, ...]:
    """Tokens split on separators (Unicode punctuation, symbols) and digit boundaries; a case boundary does not split
    (the whole word is the token, its parts are `parts`); a Ukrainian apostrophe stays in the word as `APOSTROPHE`."""
    tokens: List[Token] = []
    for raw, idx in _runs(text):
        cuts = [0, *(m.start() for m in _DIGIT_BOUNDARY.finditer(raw)), len(raw)]
        first = len(tokens)
        for a, b in zip(cuts, cuts[1:]):
            piece = raw[a:b]
            token = _normalise(piece)
            if not token:
                continue
            parts: Tuple[str, ...] = ()
            if _CASE_SPLIT.search(piece):
                parts = tuple(p for p in (_normalise(x) for x in _CASE_SPLIT.split(piece)) if p)
            tokens.append(Token(token, idx[a], idx[b - 1] + 1, parts if len(parts) > 1 else ()))
        if len(tokens) - first > 1 and len(raw) <= ALNUM_RUN_MAX:
            head = tokens[first]
            tokens[first] = Token(head.text, head.start, head.end, head.parts, _normalise(raw), tokens[-1].end,
                                  "alnum")
    return _with_compounds(text, tokens)


def _with_compounds(text: str, tokens: List[Token]) -> Tuple[Token, ...]:
    out = list(tokens)
    i = 0
    while i < len(out):
        if out[i].compound:
            i += 1
            continue
        j = i
        while j + 1 < len(out) and out[j + 1].start - out[j].end == 1 and text[out[j].end] in _HYPHENS:
            j += 1
        if j > i:
            joined = "".join(t.text for t in out[i:j + 1])
            out[i] = Token(out[i].text, out[i].start, out[i].end, out[i].parts, joined, out[j].end, "hyphen")
        i = j + 1
    return tuple(out)


def token_spans(text: str) -> Tuple[Tuple[str, int, int], ...]:
    return tuple((t.text, t.start, t.end) for t in scan(text))


# -- forms ----------------------------------------------------------------------------------------------------------

def _keep(forms: List[Optional[str]]) -> Tuple[str, ...]:
    out: List[str] = []
    for form in forms:
        if form and len(form) >= MIN_FORM_LETTERS and form not in out:
            out.append(form)
    return tuple(out)


def _strips(token: str) -> List[str]:
    """Cyrillic form (b): the token less its longest and its second-longest listed ending, each leaving at least
    MIN_FORM_LETTERS letters (review M5: здание/здания, новые/новая)."""
    out: List[str] = []
    for ending in _CYR_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= MIN_FORM_LETTERS:
            out.append(token[:-len(ending)])
            if len(out) == 2:
                break
    return out


def _without_soft(token: str) -> Optional[str]:
    return token[:-1] if token.endswith(_SOFT_FINALS) else None


def _fleeting(token: str) -> Optional[str]:
    for suffix, rest in _FLEETING:
        if token.endswith(suffix):
            return token[:-len(suffix)] + rest
    return None


def _last_vowel(token: str) -> int:
    """The index of the last vowel when only consonants follow it, else -1."""
    last = max((i for i, ch in enumerate(token) if ch in _CYR_VOWELS), default=-1)
    return last if 0 <= last < len(token) - 1 else -1


def _alternations(token: str) -> List[str]:
    """Ukrainian last-syllable alternation on stems of 4 letters or more: і -> о / е, ї -> є."""
    last = _last_vowel(token) if len(token) >= _ALTERNATION_MIN_LETTERS else -1
    if last < 0 or token[last] not in _ALTERNATIONS:
        return []
    return [token[:last] + v + token[last + 1:] for v in _ALTERNATIONS[token[last]]]


def _reverse_alternation(token: str) -> Optional[str]:
    """The nominative a Ukrainian locative stem may come from ("києв" -> "київ", "харков" -> "харків")."""
    last = _last_vowel(token)
    if last < 0 or token[last] not in _REVERSE_ALTERNATIONS:
        return None
    return token[:last] + _REVERSE_ALTERNATIONS[token[last]] + token[last + 1:]


def _cyrillic_forms(token: str) -> Tuple[str, ...]:
    """Forms (a)-(e); a word with an apostrophe also gets its forms without it ("комп'ютера" meets "компютер"),
    except a stem ending at the apostrophe: "сімʼ" links сім'я and сім'ї, but "сім" is another word."""
    forms = list(_cyrillic_forms_of(token))
    if APOSTROPHE in token:
        forms += [f.replace(APOSTROPHE, "") for f in forms if not f.endswith(APOSTROPHE)]
    return _keep(list(forms))


def _cyrillic_forms_of(token: str) -> Tuple[str, ...]:
    forms: List[Optional[str]] = []
    for base in (token, _without_soft(token)):
        if not base:
            continue
        forms += [base, *_strips(base)]
        fleeting = _fleeting(base)
        if fleeting:
            forms += [fleeting, _without_soft(fleeting)]
        for alt in _alternations(base):
            forms += [alt, *_strips(alt)]
    return _keep(forms)


def _latin_singular(word: str) -> str:
    """Latin form (b): -sses -> -ss, -ies -> -i, -es dropped after x z ch sh, else a final -s unless -ss -us -is;
    then, on 4 letters or more, a final -e dropped or a final -y turned into -i."""
    s = word
    if s.endswith("sses"):
        s = s[:-2]
    elif s.endswith("ies"):
        s = s[:-2]
    elif s.endswith("es") and s[:-2].endswith(("x", "z", "ch", "sh")):
        s = s[:-2]
    elif s.endswith("s") and not s.endswith(("ss", "us", "is")):
        s = s[:-1]
    if len(s) >= 4:
        if s.endswith("e"):
            s = s[:-1]
        elif s.endswith("y"):
            s = s[:-1] + "i"
    return s


def _is_latin(token: str) -> bool:
    return token.isascii() and any(ch.isalpha() for ch in token)


def _letters(token: str) -> int:
    return sum(ch.isalpha() for ch in token)


def is_short_function_word(token: str) -> bool:
    return token in SHORT_FUNCTION_WORDS or _strip_accents(token) in SHORT_FUNCTION_WORDS


@lru_cache(maxsize=65_536)
def token_forms(token: str) -> Tuple[str, ...]:
    """The token's native forms (§25.2.4), no truncation, no transliteration."""
    if token.isdigit():
        return (token,) if len(token) < NUMBER_MIN_DIGITS else ()
    if len(token) < MIN_FORM_LETTERS:
        if len(token) == SHORT_WORD_LETTERS and not is_short_function_word(token):
            return (_strip_accents(token) if _is_latin(_strip_accents(token)) else token,)
        return ()
    if _is_cyrillic(token):
        return _cyrillic_forms(token)
    plain = _strip_accents(token)
    if _is_latin(plain):
        return _keep([plain, _latin_singular(plain)])
    return (token,)


def primary(token: str) -> str:
    """Form (b) in the token's own script (the longest ending stripped), or the token: the part a phrase joins."""
    if _is_cyrillic(token):
        strips = [s for s in _strips(token) if not s.endswith(APOSTROPHE)]
        return strips[0] if strips else token
    plain = _strip_accents(token)
    return _latin_singular(plain) if _is_latin(plain) and len(plain) >= MIN_FORM_LETTERS else plain


# -- transliteration and the skeleton -------------------------------------------------------------------------------

def _greedy(token: str, table: Tuple[Tuple[str, str], ...], initial: Tuple[Tuple[str, str], ...] = ()) -> str:
    out: List[str] = []
    i = 0
    while i < len(token):
        if token[i] == "c" and not token.startswith("ch", i):
            out.append("с" if token[i + 1:i + 2] in _C_SOFT_BEFORE else "к")
            i += 1
            continue
        for lat, cyr in (initial if i == 0 else ()) + table:
            if token.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(token[i])
            i += 1
    return "".join(out)


def lat_to_cyr_ru(token: str) -> str:
    """Latin to Cyrillic, Russian-style (`drive_name_keys` table), `c` before e, i, y as с."""
    return _greedy(token, tuple(_LAT_TO_CYR))


def lat_to_cyr_uk(token: str) -> str:
    """Latin to Ukrainian, the national scheme in reverse (review M6): "kyiv" -> "киів", "kharkiv" -> "харків"."""
    return _greedy(token, _UK_FROM_LAT, _UK_FROM_LAT_INITIAL)


def skeleton(cyrillic: str) -> str:
    """Cyrillic letters by sound class, doubled letters collapsed: both scripts and schemes meet here."""
    s = "".join(_SKELETON.get(ch, ch) for ch in cyrillic)
    return re.sub(r"(.)\1+", r"\1", s)


def _cyr_of(token: str) -> Optional[str]:
    if _is_cyrillic(token):
        return token
    plain = _strip_accents(token)
    return lat_to_cyr_ru(_latin_singular(plain)) if _is_latin(plain) else None


@lru_cache(maxsize=65_536)
def skeleton_key(token: str) -> Optional[str]:
    """The one skeleton the index stores per word of 3 letters or more: of the Cyrillic stem (form (b)) of the word,
    a Latin word first spelled in Cyrillic (Russian-style)."""
    if _letters(token) < MIN_FORM_LETTERS or token.isdigit():
        return None
    cyr = _cyr_of(token)
    return skeleton(primary(cyr)) if cyr else None


@lru_cache(maxsize=65_536)
def query_skeletons(token: str) -> Tuple[str, ...]:
    """Every skeleton an index word this query word should meet can carry: of each Cyrillic spelling (as written;
    from Latin by both schemes), of each of its forms (a)-(e), of each form's stem, and of the nominative a Ukrainian
    locative stem comes from. Always holds `skeleton_key(token)`."""
    own = skeleton_key(token)
    if own is None:
        return ()
    plain = _strip_accents(token)
    if _is_cyrillic(token):
        spellings = [token]
    else:
        bases = [plain, _latin_singular(plain)]
        spellings = [conv(b) for b in bases for conv in (lat_to_cyr_ru, lat_to_cyr_uk)]
    out: List[str] = [own]
    for spelling in spellings:
        for form in (*_cyrillic_forms_of(spelling), spelling):
            for variant in (form, *_strips(form)):
                for v in (variant, _reverse_alternation(variant)):
                    if v and len(v) >= MIN_FORM_LETTERS and not v.endswith(APOSTROPHE):
                        for key in (skeleton(v), skeleton(primary(v))):
                            if key not in out:
                                out.append(key)
    return tuple(out)
