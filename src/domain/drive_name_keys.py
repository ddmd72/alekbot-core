"""
Lexical name keys for the name stream (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3.2, §10.1 step 3a).

NFC + casefold tokens split on separators (any Unicode punctuation, apostrophe look-alikes included) and
on case and digit boundaries. Each token gets variants as extra keys: the token without its Latin accents,
Cyrillic<->Latin transliterations (a Russian-style one both ways, and the Ukrainian national one, so
"kyiv" finds "Київ"), and character trigrams. Exact and partial names, transliteration and typos are
where a cosine over the card vector is weak. Records and queries are keyed by the same functions.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .drive_index_params import NAME_MIN_TRIGRAMS, NAME_QUERY_KEYS_MAX

_SEPARATORS = re.compile(r"[\s_\-.,;:()\[\]{}+&#@!'\"«»№/\\|~=]+")
# Not Unicode punctuation, yet typed as an apostrophe or quote (ʼ is the Ukrainian apostrophe).
_QUOTE_LIKE = frozenset("`´ʹʻʼʽ")
_BOUNDARY = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])|(?<=[a-zа-яёіїєґ])(?=[A-ZА-ЯЁІЇЄҐ])")

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "ё": "e", "є": "ye", "ж": "zh",
    "з": "z", "и": "i", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Ukrainian national transliteration (Cabinet of Ministers resolution No. 55, 2010). Word-initial forms
# differ for є ї й ю я; зг is zgh. Letters outside the Ukrainian alphabet fall back to _CYR_TO_LAT.
_UK_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
    "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ь": "", "ю": "iu", "я": "ia",
}
_UK_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}
# Latin letters NFKD does not decompose: written out as their usual unaccented spelling.
_LATIN_FOLDS = {"œ": "oe", "æ": "ae", "ø": "o", "ł": "l", "ß": "ss", "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ħ": "h"}
# Greedy, longest first. Approximate by design: trigrams absorb the rest.
_LAT_TO_CYR: List[Tuple[str, str]] = sorted([
    ("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("ye", "е"), ("yi", "ї"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
    ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"),
    ("w", "в"), ("x", "кс"), ("y", "ы"), ("z", "з"),
], key=lambda pair: -len(pair[0]))


def _is_separator(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P") or ch in _QUOTE_LIKE


def _tokens(text: str) -> List[str]:
    nfc = "".join(" " if _is_separator(ch) else ch for ch in unicodedata.normalize("NFC", text))
    out: List[str] = []
    for part in _SEPARATORS.split(nfc):
        for piece in _BOUNDARY.split(part):
            token = piece.casefold()
            if token:
                out.append(token)
    return out


def _is_cyrillic(token: str) -> bool:
    return any("а" <= ch <= "я" or ch in "ёіїєґ" for ch in token)


def _strip_accents(token: str) -> str:
    """Latin letters only: NFKD with the combining marks dropped (é→e, ñ→n, ç→c, ﬁ→fi), including a mark NFC
    leaves as its own code point (casefold("İ") is "i" + U+0307), plus _LATIN_FOLDS (œ→oe). Other scripts are
    left alone: in Cyrillic й, ё and ї are letters of their own."""
    out = []
    after_latin = False
    for ch in token:
        if unicodedata.combining(ch):
            if not after_latin:
                out.append(ch)
        elif ch in _LATIN_FOLDS:
            out.append(_LATIN_FOLDS[ch])
            after_latin = True
        elif unicodedata.name(ch, "").startswith("LATIN"):
            out.extend(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            after_latin = True
        else:
            out.append(ch)
            after_latin = False
    return unicodedata.normalize("NFC", "".join(out)).casefold()


def _translit_uk(token: str) -> Optional[str]:
    if not _is_cyrillic(token):
        return None
    out, i = [], 0
    while i < len(token):
        ch = token[i]
        if token.startswith("зг", i):
            out.append("zgh")
            i += 2
            continue
        if i == 0 and ch in _UK_INITIAL:
            out.append(_UK_INITIAL[ch])
        else:
            out.append(_UK_TO_LAT.get(ch, _CYR_TO_LAT.get(ch, ch)))
        i += 1
    return "".join(out)


def _translit(token: str) -> Optional[str]:
    if _is_cyrillic(token):
        return "".join(_CYR_TO_LAT.get(ch, ch) for ch in token)
    if token.isascii() and any(ch.isalpha() for ch in token):
        out, i = [], 0
        while i < len(token):
            for lat, cyr in _LAT_TO_CYR:
                if token.startswith(lat, i):
                    out.append(cyr)
                    i += len(lat)
                    break
            else:
                out.append(token[i])
                i += 1
        return "".join(out)
    return None


def _trigrams(token: str) -> List[str]:
    return [token[i:i + 3] for i in range(len(token) - 2)] if len(token) >= 3 else []


def _variants(token: str) -> Dict[str, bool]:
    """The token's other spellings (not the token itself), in key order: unaccented, then transliterations.
    The value says whether the spelling also gets trigrams: only the unaccented form and the Russian-style
    transliteration of the token as written. A third script form of one shared trigram (Ukrainian, or the
    transliteration of the unaccented form) would lift unrelated names over the NAME_MIN_TRIGRAMS floor."""
    bases = [token]
    stripped = _strip_accents(token)
    if stripped != token:
        bases.append(stripped)
    out: Dict[str, bool] = {}
    for base in bases:
        for variant, grams in ((base, True), (_translit(base), base == token), (_translit_uk(base), False)):
            if variant and variant != token:
                out[variant] = out.get(variant, False) or grams
    return out


def _ordered_keys(tokens: Iterable[str]) -> List[str]:
    """Whole tokens, then their variants, then the tokens' trigrams, then the variants' trigrams: a query
    capped at NAME_QUERY_KEYS_MAX loses variant trigrams first."""
    toks = list(tokens)
    variants = [_variants(t) for t in toks]
    whole = [f"w:{t}" for t in toks] + [f"w:{v}" for vs in variants for v in vs]
    grams = [f"t:{g}" for t in toks for g in _trigrams(t)]
    grams += [f"t:{g}" for vs in variants for v, gram in vs.items() if gram for g in _trigrams(v)]
    seen: Set[str] = set()
    ordered: List[str] = []
    for key in whole + grams:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def name_keys(name: str) -> Tuple[str, ...]:
    return tuple(sorted(_ordered_keys(_tokens(name))))


def query_name_keys(query: str) -> Tuple[str, ...]:
    return tuple(_ordered_keys(_tokens(query))[:NAME_QUERY_KEYS_MAX])


def name_match(record_keys: Sequence[str], query_keys: Sequence[str]) -> Optional[int]:
    """Shared keys, or None below the floor: one whole token or NAME_MIN_TRIGRAMS trigrams (§10.1)."""
    shared = set(record_keys) & set(query_keys)
    whole = sum(1 for k in shared if k.startswith("w:"))
    grams = len(shared) - whole
    if whole == 0 and grams < NAME_MIN_TRIGRAMS:
        return None
    return len(shared)


def query_token_groups(query: str) -> Tuple[FrozenSet[str], ...]:
    """One group per query token: its whole-token key and its variants' (plan Δ17)."""
    return tuple(frozenset(f"w:{t}" for t in [token, *_variants(token)]) for token in _tokens(query))


def covers_query(record_keys: Sequence[str], groups: Sequence[FrozenSet[str]]) -> bool:
    """The name contains every query token, as itself or transliterated: ranks first after RRF (plan Δ17)."""
    keys = set(record_keys)
    return bool(groups) and all(keys & group for group in groups)
