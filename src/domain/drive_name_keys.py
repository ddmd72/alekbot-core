"""
Lexical name keys for the name stream (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3.2, §10.1 step 3a).

NFC + casefold tokens split on separators and on case and digit boundaries; Cyrillic<->Latin
transliterations of each token; character trigrams. Exact and partial names, transliteration and
typos are where a cosine over the card vector is weak.
"""
from __future__ import annotations

import re
import unicodedata
from typing import FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .drive_index_params import NAME_MIN_TRIGRAMS, NAME_QUERY_KEYS_MAX

_SEPARATORS = re.compile(r"[\s_\-.,;:()\[\]{}+&#@!'\"«»№/\\|~=]+")
_BOUNDARY = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])|(?<=[a-zа-яёіїєґ])(?=[A-ZА-ЯЁІЇЄҐ])")

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "ё": "e", "є": "ye", "ж": "zh",
    "з": "z", "и": "i", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Greedy, longest first. Approximate by design: trigrams absorb the rest.
_LAT_TO_CYR: List[Tuple[str, str]] = sorted([
    ("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"),
    ("ye", "е"), ("yi", "ї"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
    ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"),
    ("w", "в"), ("x", "кс"), ("y", "ы"), ("z", "з"),
], key=lambda pair: -len(pair[0]))


def _tokens(text: str) -> List[str]:
    nfc = unicodedata.normalize("NFC", text)
    out: List[str] = []
    for part in _SEPARATORS.split(nfc):
        for piece in _BOUNDARY.split(part):
            token = piece.casefold()
            if token:
                out.append(token)
    return out


def _translit(token: str) -> Optional[str]:
    if any("а" <= ch <= "я" or ch in "ёіїєґ" for ch in token):
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


def _ordered_keys(tokens: Iterable[str]) -> List[str]:
    toks = list(tokens)
    whole = [f"w:{t}" for t in toks]
    translits = [f"w:{tr}" for t in toks if (tr := _translit(t)) and tr != t]
    grams = [f"t:{g}" for t in toks for g in _trigrams(t)]
    grams += [f"t:{g}" for t in toks if (tr := _translit(t)) and tr != t for g in _trigrams(tr)]
    seen: Set[str] = set()
    ordered: List[str] = []
    for key in whole + translits + grams:
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
    """One group per query token: its whole-token key and its transliteration's (plan Δ17)."""
    groups: List[FrozenSet[str]] = []
    for token in _tokens(query):
        tr = _translit(token)
        groups.append(frozenset({f"w:{token}"} | ({f"w:{tr}"} if tr and tr != token else set())))
    return tuple(groups)


def covers_query(record_keys: Sequence[str], groups: Sequence[FrozenSet[str]]) -> bool:
    """The name contains every query token, as itself or transliterated: ranks first after RRF (plan Δ17)."""
    keys = set(record_keys)
    return bool(groups) and all(keys & group for group in groups)
