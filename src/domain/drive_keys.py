"""
Drive keys — the one normaliser of Search v2 (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4, §25.5.1, §25.13.4,
§25.13.5). Pure: stdlib only, no I/O. The index and the query build every key with these functions (P3), so a form
the index stored is a form the query emits.

Public interface:
    token_spans(text) -> ((token, start, end), ...)   NFC + casefold tokens with their spans in `text`
    token_forms(token) -> (form, ...)          the token's own forms (a)-(e), each of 3 letters or more; a digit
                                               token under 3 digits is its own form, a longer one has none (`n:`)
    all_forms(token) -> (form, ...)            own forms, then the forms of their transliterations
    word_form_keys(text) -> (key, ...)         `w:` keys of every token of `text` (passage keywords)
    word_keys(text), word_keys_capped(text)    a chunk's `w:`, `n:`, `m:` keys, earliest text first, at most
                                               WORD_KEYS_PER_CHUNK (and whether keys were cut)
    phrase_keys(text) -> (key, ...)            `e:` keys of a named thing's spelling
    family_keys(prefix, words) -> (key, ...)   the forms of every token of `words` under one prefix
    about_keys_of(rec), about_keys_capped(rec)   a record's about keys, at most ABOUT_KEYS_PER_RECORD
    about_sources(rec) -> {key: (AboutSource, ...)}   which field gave each about key (render time, not stored)
    source_class_of_about_key(key) -> SourceClass | None    the class an about key proves, from its prefix alone
    query_concepts(drive_query) -> QueryConcepts      §25.5.1
    match_spans(text, keys) -> ((start, end), ...)    where any of a concept's `w:`/`n:`/`m:` keys occurs in `text`
    name_keys_v2(name) -> (key, ...)           the v1 name keys plus `w:` forms and `n:`/`y:` digit forms
    index_tables_digest() -> str               hash of every table an index key depends on (pinned per version)
    FUNCTION_WORDS, GENERIC_WORDS, NAME_KEYS_V2, PHOTO_OBJECT, PHOTO_NUMBER, AUTOMATIC_FAMILIES

Verified and automatic about keys never share a prefix (arbiter M3): a photo object's phrase keys are `po:` (not
`e:`) and a photo's visible-text numbers `pn:` (not `n:`), so `source_class_of_about_key` decides a tier from the
lean record's keys without reading the record. `l:` keys have no class: no query produces them.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from functools import lru_cache
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_content_terms import (
    _GROUP_MIN_DIGITS, _MIN_DIGITS, _MONTH, _MONTH_FORMS, _MONTH_OF, CONTENT_TERMS_PER_CHUNK, clip_query,
    content_terms, query_term_spans, term_spans,
)
from .drive_content_terms import _AMOUNT, _DAY_MONTH_YEAR, _DMY, _GROUPS, _ID, _MONTH_DAY_YEAR, _YMD
from .drive_index import TriageVerdict
from .drive_index_params import (
    ABOUT_KEYS_PER_RECORD, CONCEPT_KEYS_MAX, MAX_CONCEPTS, QUERY_TERM_CHARS, WORD_KEYS_PER_CHUNK,
)
from .drive_key_sources import AboutField, AboutSource, KeyedRecord
from .drive_labels import KIND_WORDS, PHOTO_KIND_WORDS
from .drive_match import Concept, KeyFamily, QueryConcepts, SourceClass
from .drive_name_keys import (
    _BOUNDARY, _CYR_TO_LAT, _LAT_TO_CYR, _LATIN_FOLDS, _QUOTE_LIKE, _UK_INITIAL, _UK_TO_LAT, _is_cyrillic,
    _strip_accents, _translit_uk, name_keys,
)

NAME_KEYS_V2 = 2                 # the NAME_KEYS_VERSION `name_keys_v2` is made for; P5 sets it behind the gate
PHOTO_OBJECT = "po:"             # a photo object's phrase keys: automatic, kept apart from verified `e:` (M3)
PHOTO_NUMBER = "pn:"             # number terms of a photo's visible text: automatic, kept apart from `n:` (M3)
# The automatic and kind families a query word is sent under, in order (§25.5.1 step 5; po: by M3).
AUTOMATIC_FAMILIES: Tuple[str, ...] = (
    KeyFamily.KIND.value, KeyFamily.LABEL_WORD.value, KeyFamily.TOPIC_WORD.value, KeyFamily.DOMAIN_WORD.value,
    KeyFamily.VALIDITY_WORD.value, KeyFamily.PHOTO_WORD.value, PHOTO_OBJECT,
)

# -- the form tables (hashed by index_tables_digest) ---------------------------------------------------------------
MIN_FORM_LETTERS = 3
_CYR_ENDINGS: Tuple[str, ...] = tuple(sorted((
    "ами ями ого его ому ему ой ей ий ый ым им ых их ая яя ое ее ую юю ою ею ові еві ом ем ах ях ам ям ов ев ы и а я "
    "у ю е є і ї о").split(), key=lambda e: (-len(e), e)))
_SOFT_FINALS: Tuple[str, ...] = ("й", "ь")
_FLEETING: Tuple[Tuple[str, str], ...] = (("ень", "нь"), ("ок", "к"), ("ек", "к"), ("ец", "ц"))
_ALTERNATIONS: Mapping[str, Tuple[str, ...]] = {"і": ("о", "е"), "ї": ("є",)}
_ALTERNATION_MIN_LETTERS = 4
_CYR_VOWELS = frozenset("аеиіїоуюяєыэё")
_C_SOFT_BEFORE = ("e", "i", "y")     # Latin `c` before these is с, elsewhere к (general Latin orthography)
_YEAR_MIN, _YEAR_MAX = 1900, 2099
# Caps on what one record contributes (§25.2.6): the earliest entries are kept.
ENTITIES_MAX, ENTITY_FORMS_MAX, KEY_VALUES_MAX, DATES_MAX, WORD_LIST_MAX = 12, 8, 12, 5, 24
PHOTO_OBJECTS_MAX, PHOTO_OBJECT_FORMS_MAX = 8, 6

# -- query-only word lists (not hashed: they change no stored key) ------------------------------------------------
FUNCTION_WORDS: FrozenSet[str] = frozenset("""
a an the of from my your his her its our their me you he she it we they this that these those in on at for and or
but with to by about into onto over under as is are was were be been am do does did have has had what which who whom
whose where when why how there here any some no not
и в во на с со из от по для о об обо у к ко мой моя моё мое мои твой твоя твои я мне меня ты он она оно они мы вы
его её ее их это этот эта эти тот та те что где когда как какой какая какие кто чем при за над под про между без
до после или но а же ли не ни бы есть был была были
і й та з із зі від до про при під між або але чи ні мій моє мої я мені мене ти він вона воно вони ми ви її їх це
цей ця ці той ті що де коли як який яка які хто є
el la los las un una unos unas de del al en con por para sobre y o u e pero que qué donde dónde cuando cuándo
como cómo cual cuál quien quién mi mis tu tus su sus me te se lo le les es son está están hay este esta estos estas
ese esa eso
le les une des du au aux à dans sur pour par avec et ou mais qui quoi où quand comment mon ma mes ton ta tes sa ses
je il elle nous vous ils elles ce cet cette ces est sont ne pas
""".split()) - {"son"}   # "son" (fr possessive) is an English noun
GENERIC_WORDS: FrozenSet[str] = frozenset("""
all everything anything something every file files document documents doc docs stuff thing things item items info
information find search show look list give get need want please related regarding concerning
всё все весь вся всех всего что-нибудь нибудь файл файлы файла файлов документ документы документа документов найди
найти покажи показать поищи ищи информация информацию связанное связанные касается нужно нужны хочу пожалуйста
усе усі всі файли файлів документи документів знайди знайти пошукай шукай інформація інформацію потрібно будь
ласка
todo todos toda todas archivo archivos fichero ficheros documento documentos busca buscar encuentra encontrar
muestra mostrar información informacion
tout tous toute toutes fichier fichiers trouve trouver cherche chercher montre montrer
""".split())

_MONTH_YEAR = re.compile(rf"(?<![\w]){_MONTH},?\s+(?:(?:de|del)\s+)?(\d{{4}})(?!\d)", re.IGNORECASE)

Span = Tuple[int, int]
_Item = Tuple[str, AboutSource]


# -- tokens ---------------------------------------------------------------------------------------------------------

def _is_separator(ch: str) -> bool:
    """Unicode punctuation, symbols, separators and controls, and apostrophe look-alikes (as `drive_name_keys`,
    plus symbols: "100€" is two tokens)."""
    return unicodedata.category(ch)[0] in "PSZC" or ch in _QUOTE_LIKE or ch.isspace()


def token_spans(text: str) -> Tuple[Tuple[str, int, int], ...]:
    """Tokens split as `drive_name_keys` splits (case and digit boundaries, a hyphen separates), each NFC + casefold,
    with its `[start, end)` in the original `text` (decomposed text included)."""
    out: List[Tuple[str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if _is_separator(text[i]):
            i += 1
            continue
        j = i
        while j < n and not _is_separator(text[j]):
            j += 1
        run = text[i:j]
        cuts = [0, *(m.start() for m in _BOUNDARY.finditer(run)), len(run)]
        for a, b in zip(cuts, cuts[1:]):
            token = unicodedata.normalize("NFC", run[a:b]).casefold()
            if token:
                out.append((token, i + a, i + b))
        i = j
    return tuple(out)


def _tokens(text: str) -> List[str]:
    return [t for t, _, _ in token_spans(text)]


# -- forms ----------------------------------------------------------------------------------------------------------

def _keep(forms: Iterable[Optional[str]]) -> Tuple[str, ...]:
    out: List[str] = []
    for form in forms:
        if form and (len(form) >= MIN_FORM_LETTERS or form.isdigit()) and form not in out:
            out.append(form)
    return tuple(out)


def _strip_ending(token: str) -> Optional[str]:
    """Cyrillic form (b): the longest listed ending that leaves at least MIN_FORM_LETTERS letters, stripped."""
    for ending in _CYR_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= MIN_FORM_LETTERS:
            return token[:-len(ending)]
    return None


def _without_soft(token: str) -> Optional[str]:
    return token[:-1] if token.endswith(_SOFT_FINALS) else None


def _fleeting(token: str) -> Optional[str]:
    for suffix, rest in _FLEETING:
        if token.endswith(suffix):
            return token[:-len(suffix)] + rest
    return None


def _alternations(token: str) -> List[str]:
    """Ukrainian last-syllable alternation on stems of 4 letters or more: the last vowel і becomes о / е, ї becomes
    є, when only consonants follow it ("київ" -> "києв", "харків" -> "харков")."""
    if len(token) < _ALTERNATION_MIN_LETTERS:
        return []
    last = max((i for i, ch in enumerate(token) if ch in _CYR_VOWELS), default=-1)
    if last < 0 or last == len(token) - 1 or token[last] not in _ALTERNATIONS:
        return []
    return [token[:last] + v + token[last + 1:] for v in _ALTERNATIONS[token[last]]]


def _cyrillic_forms(token: str) -> Tuple[str, ...]:
    forms: List[Optional[str]] = []
    soft = _without_soft(token)
    for base in (token, soft):
        if not base:
            continue
        forms += [base, _strip_ending(base)]
        fleeting = _fleeting(base)
        if fleeting:
            forms += [fleeting, _without_soft(fleeting)]
        for alt in _alternations(base):
            forms += [alt, _strip_ending(alt)]
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


def _latin_forms(token: str) -> Tuple[str, ...]:
    plain = _strip_accents(token)
    return _keep([plain, _latin_singular(plain)])


def _primary(token: str) -> str:
    """Form (b) in the token's own script, or the token: the form a phrase joins (§25.2.4 `e:`)."""
    if _is_cyrillic(token):
        return _strip_ending(token) or token
    plain = _strip_accents(token)
    if _is_latin(plain):
        return _latin_singular(plain)
    return token


def _translit_ru(token: str) -> str:
    return "".join(_CYR_TO_LAT.get(ch, ch) for ch in token)


def _lat_to_cyr(token: str) -> str:
    """Latin to Cyrillic, greedy longest first (`drive_name_keys`), with `c` before e, i, y as с."""
    out: List[str] = []
    i = 0
    while i < len(token):
        if token[i] == "c" and not token.startswith("ch", i):
            out.append("с" if token[i + 1:i + 2] in _C_SOFT_BEFORE else "к")
            i += 1
            continue
        for lat, cyr in _LAT_TO_CYR:
            if token.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(token[i])
            i += 1
    return "".join(out)


@lru_cache(maxsize=65_536)
def token_forms(token: str) -> Tuple[str, ...]:
    """The token's own forms (a)-(e) (§25.2.4), each of MIN_FORM_LETTERS letters or more, no truncation."""
    if token.isdigit():
        return (token,) if len(token) < _MIN_DIGITS else ()
    if _is_cyrillic(token):
        return _cyrillic_forms(token)
    if _is_latin(_strip_accents(token)):
        return _latin_forms(token)
    return _keep([token])


@lru_cache(maxsize=65_536)
def all_forms(token: str) -> Tuple[str, ...]:
    """Own forms, then the transliterations of forms (a) and (b) with their target script's forms (a)-(b):
    Russian-style and Ukrainian national for Cyrillic, Latin-to-Cyrillic (the `c` rule) for Latin."""
    own = token_forms(token)
    extra: List[str] = []
    if _is_cyrillic(token):
        for base in _keep([token, _strip_ending(token)]):
            for lat in (_translit_ru(base), _translit_uk(base) or ""):
                extra += _latin_forms(lat) if _is_latin(lat) else ()
    else:
        plain = _strip_accents(token)
        if _is_latin(plain) and not token.isdigit():
            for base in _keep([plain, _latin_singular(plain)]):
                cyr = _lat_to_cyr(base)
                extra += _keep([cyr, _strip_ending(cyr)])
    return _keep([*own, *extra])


# -- families -------------------------------------------------------------------------------------------------------

def _phrase_strings(text: str) -> List[str]:
    """A named thing's spellings: one token gets its full form set; several join each token's form (b) with `_`,
    one string per script variant (as written, Latin by the Russian-style and the Ukrainian transliteration,
    Cyrillic by Latin-to-Cyrillic). Articles are kept."""
    toks = _tokens(text)
    if len(toks) == 1:
        return list(all_forms(toks[0]))
    if not toks:
        return []
    own = [_primary(t) for t in toks]
    variants = [
        own,
        [_primary(_translit_ru(p)) if _is_cyrillic(p) else p for p in own],
        [_primary(_translit_uk(p) or p) if _is_cyrillic(p) else p for p in own],
        [_primary(_lat_to_cyr(p)) if _is_latin(p) else p for p in own],
    ]
    out: List[str] = []
    for parts in variants:
        joined = "_".join(parts)
        if joined not in out:
            out.append(joined)
    return out


def phrase_keys(text: str) -> Tuple[str, ...]:
    return tuple(KeyFamily.ENTITY.value + s for s in _phrase_strings(text))


def family_keys(prefix: str, words: Iterable[str]) -> Tuple[str, ...]:
    """The forms of every token of `words` under `prefix` (`k:`, `lw:`, `tw:`, `dw:`, `lv:`, `pw:`), in order."""
    out: Dict[str, None] = {}
    for word in words:
        for token in _tokens(word):
            for form in all_forms(token):
                out[prefix + form] = None
    return tuple(out)


def word_form_keys(text: str) -> Tuple[str, ...]:
    """`w:` keys of every token of `text` (a passage keyword's keys, §25.4.2)."""
    return family_keys(KeyFamily.WORD.value, [text])


# -- chunk keys and spans -------------------------------------------------------------------------------------------

def _year_ok(year: int) -> bool:
    return _YEAR_MIN <= year <= _YEAR_MAX


def _month_year_spans(text: str) -> List[Tuple[int, int, str]]:
    """Month-year spans ("March 2023", "березня 2023", "marzo de 2023") as (start, end, "YYYY-MM")."""
    out = []
    for m in _MONTH_YEAR.finditer(text):
        year = int(m.group(2))
        if _year_ok(year):
            out.append((m.start(), m.end(), f"{year:04d}-{_MONTH_OF[m.group(1).casefold()]:02d}"))
    return out


def _text_items(text: str) -> List[Tuple[int, int, str]]:
    """Every `w:`, `n:`, `m:` key of `text` with the span it came from, in text order."""
    items: List[Tuple[int, int, str]] = []
    for token, start, end in token_spans(text):
        items += [(start, end, KeyFamily.WORD.value + f) for f in all_forms(token)]
    kept_terms = set(content_terms(text))       # the per-chunk 256-term cap of the normaliser
    for start, end, forms, category in term_spans(text):
        items += [(start, end, KeyFamily.NUMBER.value + f) for f in forms if f in kept_terms]
        if category == "date":
            items += [(start, end, KeyFamily.MONTH.value + f[:7]) for f in forms]
    items += [(start, end, KeyFamily.MONTH.value + ym) for start, end, ym in _month_year_spans(text)]
    items.sort(key=lambda item: item[0])
    return items


def word_keys_capped(text: str) -> Tuple[Tuple[str, ...], bool]:
    """A chunk's keys and whether the cap cut some: past WORD_KEYS_PER_CHUNK the earliest text's keys are kept."""
    keys = list(dict.fromkeys(key for _, _, key in _text_items(text)))
    return tuple(keys[:WORD_KEYS_PER_CHUNK]), len(keys) > WORD_KEYS_PER_CHUNK


def word_keys(text: str) -> Tuple[str, ...]:
    """A chunk's `word_keys`: `w:`, `n:`, `m:` of its text (§25.2.2)."""
    return word_keys_capped(text)[0]


def match_spans(text: str, keys: Sequence[str]) -> Tuple[Span, ...]:
    """The spans of `text` where any of `keys` (one concept's `w:`, `n:` or `m:` keys) occurs, sorted and unique:
    the input of the proximity window (§25.5.4), from the same tokeniser as the keys."""
    wanted = set(keys)
    return tuple(sorted({(start, end) for start, end, key in _text_items(text) if key in wanted}))


# -- record keys ----------------------------------------------------------------------------------------------------

_VERIFIED = SourceClass.VERIFIED_RECORD
_AUTOMATIC = SourceClass.AUTOMATIC_RECORD
_ABOUT_CLASS: Mapping[str, SourceClass] = {
    KeyFamily.ENTITY.value: _VERIFIED, KeyFamily.VALUE.value: _VERIFIED, KeyFamily.YEAR.value: _VERIFIED,
    KeyFamily.MONTH.value: _VERIFIED, KeyFamily.KIND.value: _VERIFIED,
    KeyFamily.LABEL_WORD.value: _AUTOMATIC, KeyFamily.TOPIC_WORD.value: _AUTOMATIC,
    KeyFamily.DOMAIN_WORD.value: _AUTOMATIC, KeyFamily.VALIDITY_WORD.value: _AUTOMATIC,
    KeyFamily.PHOTO_WORD.value: _AUTOMATIC, PHOTO_OBJECT: _AUTOMATIC, PHOTO_NUMBER: _AUTOMATIC,
}


def source_class_of_about_key(key: str) -> Optional[SourceClass]:
    """The class an about key proves, from its prefix alone (M3); None for `l:` and non-about prefixes."""
    return _ABOUT_CLASS.get(key[:key.index(":") + 1]) if ":" in key else None


def _date_items(year: int, month: int, field: AboutField, detail: str, printed: str) -> List[_Item]:
    source = AboutSource(field, detail, printed, _VERIFIED)
    return [(f"{KeyFamily.YEAR.value}{year:04d}", source), (f"{KeyFamily.MONTH.value}{year:04d}-{month:02d}", source)]


def _about_items(rec: KeyedRecord) -> List[_Item]:
    """Every about key with its source, earliest kept first under the cap: labels, kind, verified fields, then the
    automatic families."""
    items: List[_Item] = []

    def add(keys: Iterable[str], field: AboutField, detail: str, printed: str, cls: Optional[SourceClass]) -> None:
        source = AboutSource(field, detail, printed, cls)
        items.extend((key, source) for key in keys)

    def add_words(prefix: str, words: Sequence[str], field: AboutField, detail: str = "") -> None:
        for word in words[:WORD_LIST_MAX]:
            add(family_keys(prefix, [word]), field, detail, word, _ABOUT_CLASS[prefix])

    profile = rec.profile
    if profile is not None:
        add([f"{KeyFamily.LABEL.value}type:{profile.doc_type}"], AboutField.LABEL, "type", profile.doc_type, None)
        for domain in profile.life_domains:
            add([f"{KeyFamily.LABEL.value}domain:{domain}"], AboutField.LABEL, "domain", domain, None)
    if rec.valid_until is not None:
        add([f"{KeyFamily.LABEL.value}valid"], AboutField.LABEL, "valid", rec.valid_until.isoformat()[:10], None)

    kind_words = list(KIND_WORDS.get(rec.kind, ()))
    if rec.kind == "image" and rec.triage_verdict is TriageVerdict.PHOTO:
        kind_words += PHOTO_KIND_WORDS
    add_words(KeyFamily.KIND.value, kind_words, AboutField.KIND, rec.kind)

    if profile is not None:
        for entity in profile.entities[:ENTITIES_MAX]:
            for spelling in (entity.name, entity.printed, *entity.forms[:ENTITY_FORMS_MAX]):
                add(phrase_keys(spelling), AboutField.ENTITY, entity.type, spelling, _VERIFIED)
    for place in rec.place_forms:
        add(phrase_keys(place), AboutField.PLACE, "", place, _VERIFIED)
    if profile is not None:
        for kv in profile.key_values[:KEY_VALUES_MAX]:
            canonical = kv.canonical.strip().casefold()
            if canonical:
                add([KeyFamily.VALUE.value + canonical], AboutField.VALUE, kv.label, kv.printed, _VERIFIED)
        for d in profile.dates[:DATES_MAX]:
            iso = d.iso.isoformat()
            add([KeyFamily.VALUE.value + iso], AboutField.DATE, d.label, iso, _VERIFIED)
            items.extend(_date_items(d.iso.year, d.iso.month, AboutField.DATE, d.label, iso))
    if rec.effective_date is not None:
        eff = rec.effective_date
        items.extend(_date_items(eff.year, eff.month, AboutField.EFFECTIVE_DATE, "", eff.isoformat()[:10]))

    if profile is not None:
        add_words(KeyFamily.LABEL_WORD.value, profile.label_words, AboutField.LABEL_WORD)
        add_words(KeyFamily.TOPIC_WORD.value, profile.topic_words, AboutField.TOPIC_WORD)
        add_words(KeyFamily.DOMAIN_WORD.value, profile.domain_words, AboutField.DOMAIN_WORD)
        if rec.valid_until is not None:
            add_words(KeyFamily.VALIDITY_WORD.value, profile.validity_words, AboutField.VALIDITY_WORD)

    objects = rec.photo_objects[:PHOTO_OBJECTS_MAX]
    for obj in objects:
        for spelling in (obj.name, *obj.forms[:PHOTO_OBJECT_FORMS_MAX]):
            add([PHOTO_OBJECT + s for s in _phrase_strings(spelling)], AboutField.PHOTO_OBJECT, "", spelling,
                _AUTOMATIC)
    pw = KeyFamily.PHOTO_WORD.value
    for detail, text in (("description", rec.photo_description), ("scene", rec.photo_scene),
                         *(("object", obj.name) for obj in objects), ("visible_text", rec.visible_text)):
        if text:
            add(family_keys(pw, [text]), AboutField.PHOTO_WORD, detail, text, _AUTOMATIC)
    if rec.visible_text:
        for term in content_terms(rec.visible_text):
            add([PHOTO_NUMBER + term], AboutField.VISIBLE_TEXT_NUMBER, "", term, _AUTOMATIC)
    return items


def about_keys_capped(rec: KeyedRecord) -> Tuple[Tuple[str, ...], bool]:
    """The record's about keys and whether ABOUT_KEYS_PER_RECORD cut some (the earliest entries are kept)."""
    keys = list(dict.fromkeys(key for key, _ in _about_items(rec)))
    return tuple(keys[:ABOUT_KEYS_PER_RECORD]), len(keys) > ABOUT_KEYS_PER_RECORD


def about_keys_of(rec: KeyedRecord) -> Tuple[str, ...]:
    """`about_keys` (§25.2.4): `e:`, `v:`, `y:`, `m:`, `k:`, `lw:`, `tw:`, `dw:`, `lv:`, `pw:`, `po:`, `pn:`, `l:`."""
    return about_keys_capped(rec)[0]


def about_sources(rec: KeyedRecord) -> Mapping[str, Tuple[AboutSource, ...]]:
    """Which field gave each about key the record keeps (recomputed at render time, never stored)."""
    kept = set(about_keys_of(rec))
    out: Dict[str, List[AboutSource]] = {}
    for key, source in _about_items(rec):
        if key in kept and source not in out.setdefault(key, []):
            out[key].append(source)
    return {key: tuple(sources) for key, sources in out.items()}


# -- the query ------------------------------------------------------------------------------------------------------

def _is_removable(token: str) -> bool:
    plain = _strip_accents(token)
    return any(t in FUNCTION_WORDS or t in GENERIC_WORDS for t in (token, plain))


def _cut(keys: Iterable[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(keys))[:CONCEPT_KEYS_MAX]


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _number_concept(word: str, forms: Sequence[str]) -> Concept:
    about: List[str] = []
    for f in forms:
        about += [KeyFamily.NUMBER.value + f, KeyFamily.VALUE.value + f]
    about += [KeyFamily.YEAR.value + f for f in forms if len(f) == 4 and f.isdigit() and _year_ok(int(f))]
    about += [PHOTO_NUMBER + f for f in forms]
    return Concept(word=word, about_keys=_cut(about), word_keys=_cut(KeyFamily.NUMBER.value + f for f in forms),
                   hint_keys=(), is_number=True)


def _numbers(text: str) -> Tuple[List[Tuple[int, Concept]], List[Span]]:
    """Number and month concepts in text order with their start, and the stretches they consume (§25.5.1 step 3):
    a date claims first, a month name with a year next, then amounts, digit groups and ids outside both."""
    spans = query_term_spans(text)
    dates = [(s, e) for s, e, _, cat in spans if cat == "date"]
    months = [(s, e, ym) for s, e, ym in _month_year_spans(text) if not any(_overlaps((s, e), d) for d in dates)]
    found: List[Tuple[int, int, Concept]] = []
    for s, e, forms, cat in spans:
        if forms and (cat == "date" or not any(_overlaps((s, e), (ms, me)) for ms, me, _ in months)):
            found.append((s, e, _number_concept(text[s:e].strip().casefold(), forms)))
    for s, e, ym in months:
        key = (KeyFamily.MONTH.value + ym,)
        found.append((s, e, Concept(word=text[s:e].strip().casefold(), about_keys=key, word_keys=key, hint_keys=(),
                                    is_number=True)))
    found.sort(key=lambda f: f[0])
    seen: Set[Tuple[str, ...]] = set()
    concepts: List[Tuple[int, Concept]] = []
    for s, _, concept in found:
        if concept.word_keys not in seen:
            seen.add(concept.word_keys)
            concepts.append((s, concept))
    return concepts, [(s, e) for s, e, _ in found]


def _run_strings(tokens: Sequence[str], i: int) -> List[str]:
    """The phrase strings of the 2- and 3-token runs holding token `i`, shorter first; a run ending in a function
    word names nothing and is skipped."""
    out: List[str] = []
    for length in (2, 3):
        for start in range(i - length + 1, i + 1):
            end = start + length
            if start < 0 or end > len(tokens) or tokens[end - 1] in FUNCTION_WORDS:
                continue
            out += _phrase_strings(" ".join(tokens[start:end]))
    return out


def _word_concept(token: str, runs: Sequence[str]) -> Concept:
    """Keys in the §25.5.1 step 5 order, each list cut at CONCEPT_KEYS_MAX: own forms, the runs' phrases,
    transliterations, then the kind and automatic families (own forms first), photo-object runs last."""
    own = token_forms(token)
    trans = tuple(f for f in all_forms(token) if f not in own)
    e = KeyFamily.ENTITY.value
    words = _cut(KeyFamily.WORD.value + f for f in own + trans)
    about = [e + f for f in own] + [e + r for r in runs] + [e + f for f in trans]
    about += [fam + f for f in own for fam in AUTOMATIC_FAMILIES]
    about += [PHOTO_OBJECT + r for r in runs]
    about += [fam + f for f in trans for fam in AUTOMATIC_FAMILIES]
    return Concept(word=token, about_keys=_cut(about), word_keys=words, hint_keys=words, is_number=False)


def query_concepts(drive_query: str) -> QueryConcepts:
    """`drive_query` as strict-AND concepts (§25.5.1): numbers first, then words in query order, at most
    MAX_CONCEPTS. Function and generic words, and words too short to have a form, are `ignored`; concepts past the
    cap are `cut`. Phrase runs are built over every token before anything is removed."""
    text = clip_query(drive_query, QUERY_TERM_CHARS)
    numbers, consumed = _numbers(text)
    spans = token_spans(text)
    tokens = [t for t, _, _ in spans]
    ignored: List[str] = []
    words: List[Concept] = []
    seen: Set[str] = set()
    for i, (token, start, end) in enumerate(spans):
        if any(_overlaps((start, end), c) for c in consumed):
            continue
        if _is_removable(token) or not all_forms(token):
            if token not in ignored:
                ignored.append(token)
            continue
        if token not in seen:
            seen.add(token)
            words.append(_word_concept(token, _run_strings(tokens, i)))
    ordered = [c for _, c in numbers] + words
    return QueryConcepts(concepts=tuple(ordered[:MAX_CONCEPTS]), ignored=tuple(ignored),
                         cut=tuple(c.word for c in ordered[MAX_CONCEPTS:]))


# -- name keys v2 ---------------------------------------------------------------------------------------------------

def name_keys_v2(name: str) -> Tuple[str, ...]:
    """The v1 name keys (whole tokens, variants, trigrams) plus every token's `w:` forms and the `n:`/`y:` forms of
    digit tokens and of the name's number terms, with and without its extension (§25.2.4, NAME_KEYS_VERSION 2)."""
    keys = set(name_keys(name))
    for token, _, _ in token_spans(name):
        keys.update(KeyFamily.WORD.value + f for f in all_forms(token))
        if token.isdigit() and len(token) >= _MIN_DIGITS:
            keys.add(KeyFamily.NUMBER.value + token)
            if len(token) == 4 and _year_ok(int(token)):
                keys.add(KeyFamily.YEAR.value + token)
    stem = os.path.splitext(name)[0]
    keys.update(KeyFamily.NUMBER.value + t for t in (*content_terms(name), *content_terms(stem)))
    return tuple(sorted(keys))


# -- the version pin ------------------------------------------------------------------------------------------------

def index_tables_digest() -> str:
    """SHA-256 over every table and constant an index key depends on (forms, transliteration, kind words, the
    number-term normaliser's patterns and caps). A test pins it per (KEYS_VERSION, NAME_KEYS_V2)."""
    tables = (
        MIN_FORM_LETTERS, _CYR_ENDINGS, _SOFT_FINALS, _FLEETING, sorted(_ALTERNATIONS.items()),
        _ALTERNATION_MIN_LETTERS, sorted(_CYR_VOWELS), _C_SOFT_BEFORE, (_YEAR_MIN, _YEAR_MAX),
        (ENTITIES_MAX, ENTITY_FORMS_MAX, KEY_VALUES_MAX, DATES_MAX, WORD_LIST_MAX, PHOTO_OBJECTS_MAX,
         PHOTO_OBJECT_FORMS_MAX, WORD_KEYS_PER_CHUNK, ABOUT_KEYS_PER_RECORD),
        sorted(_CYR_TO_LAT.items()), sorted(_UK_TO_LAT.items()), sorted(_UK_INITIAL.items()), _LAT_TO_CYR,
        sorted(_LATIN_FOLDS.items()), sorted((k, v) for k, v in KIND_WORDS.items()), PHOTO_KIND_WORDS,
        sorted(_MONTH_FORMS.items()), _MONTH_YEAR.pattern, _BOUNDARY.pattern, sorted(_QUOTE_LIKE),
        CONTENT_TERMS_PER_CHUNK, _MIN_DIGITS, _GROUP_MIN_DIGITS,
        _AMOUNT.pattern, _DMY.pattern, _YMD.pattern, _ID.pattern, _GROUPS.pattern, _DAY_MONTH_YEAR.pattern,
        _MONTH_DAY_YEAR.pattern, PHOTO_OBJECT, PHOTO_NUMBER,
    )
    return hashlib.sha256(repr(tables).encode("utf-8")).hexdigest()
