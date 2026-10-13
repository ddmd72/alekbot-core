"""
Shared spelling helpers of the drive keyers (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4).

What is left of the v1 name keys (tag `drive-search-v1`): the Latin accent folding, the Cyrillic test, the
Russian-style Latin-to-Cyrillic table, the quote look-alikes, the case and digit boundary and trigrams, which `drive_key_forms` and
`drive_keys` (name keys v2) build on. The v1 name-key functions (`name_keys`, `query_name_keys`, `name_match`,
`query_token_groups`, `covers_query`) were removed with Search v2: no caller in `src/` used them.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

# Not Unicode punctuation, yet typed as an apostrophe or quote (ʼ is the Ukrainian apostrophe).
_QUOTE_LIKE = frozenset("`´ʹʻʼʽ")
_BOUNDARY = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])|(?<=[a-zа-яёіїєґ])(?=[A-ZА-ЯЁІЇЄҐ])")

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


def _trigrams(token: str) -> List[str]:
    return [token[i:i + 3] for i in range(len(token) - 2)] if len(token) >= 3 else []
