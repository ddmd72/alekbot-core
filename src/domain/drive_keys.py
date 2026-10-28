"""
Drive keys — the one normaliser of Search v2 (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4, §25.5.1, §25.13.4,
§25.13.5). Pure: stdlib only, no I/O. The index and the query build every key with these functions (P3).

Public interface:
    token_spans(text) -> ((token, start, end), ...)   (from drive_key_forms)
    token_forms(token) -> (form, ...)          native forms (from drive_key_forms); all_forms is the same set
    word_form_keys(text) -> (key, ...)         `w:` keys of every token of `text` (passage keywords, hint keys)
    word_keys(text), word_keys_capped(text)    a chunk's `w:`, `x:`, `n:`, `m:` keys, earliest text first, at most
                                               WORD_KEYS_PER_CHUNK (and whether keys were cut)
    phrase_keys(text) -> (key, ...)            `e:` and `ex:` keys of a named thing's spelling
    family_keys(prefix, words) -> (key, ...)   the native forms of every token of `words` under one prefix
    about_keys_of(rec), about_keys_capped(rec)   a record's about keys, at most ABOUT_KEYS_PER_RECORD
    about_sources(rec) -> {key: (AboutSource, ...)}   which field gave each about key (render time, not stored)
    source_class_of_about_key / _word_key / _name_key(key) -> SourceClass | None   from the prefix alone
    query_concepts(drive_query) -> QueryConcepts      §25.5.1; each concept's key lists are UNCUT (coverage)
    fetch_batches(keys) -> ((key, ...), ...)   a key list cut into fetches of at most CONCEPT_KEYS_MAX (review B1)
    match_spans(text, keys) -> ((start, end), ...)    where any of a concept's word keys occurs in `text`
    name_keys_v2(name), query_name_keys_v2(query)     name keys v2 (NAME_KEYS_V2) and the name stream's query keys
    name_word_concepts(query) -> (Concept, ...)       a query left with no concept, its words searched by name (xc F2)
    note_keys(text, doc_type, life_areas) -> (key, ...)   the owner note's keys (DRIVE_FILE_NOTES_RFC §5.4), at most
                                               NOTE_KEYS_MAX; a record's copy of the note leads its about keys
    index_tables_digest(), golden_digest()     version pins: the tables, and the rules' output on a fixed fixture
    note_keys_digest()                         the note keyer's pin, per NOTE_MERGE_VERSION (never in the two above)
    FUNCTION_WORDS, GENERIC_WORDS, TIME_WORDS, NAME_KEYS_V2

Families (§25.2.4, review M8): native-script forms and the spellings a file prints are `w:` (chunks, names) and `e:`
(records): exact. An entity spelling the profiler wrote that the file does not print is `ef:`: a translation (xc F1).
A transliteration crosses scripts and spellings through ONE skeleton per word on the index — `x:` (a Cyrillic word) or
`xl:` (a Latin word) in chunks and names, `ex:` / `exl:` in records — and is a translation (the By-translation tier),
never exact. A Latin query word never reads a Latin word's skeleton (`xl:`, `exl:`): two Latin words meeting through a
Cyrillic stem ("Mario" and "Maria") are not a translation, and Latin-to-Latin spelling is the `w:` forms' job (xc F3).
A photo object's phrases are `pe:` and a photo's visible-text numbers `pn:`: automatic, so a key's class follows from
its prefix alone. The D31 tables name the profile's own doc type (`lw:`) and life areas (`dw:`) in four languages. A
file's capture (§26.3, Δ58) is keyed by the CAPTURE_NAMES table as phrases, like the `k:` table: `cp:` when code
detected it (verified), `cpa:` when a model said it (automatic); a query word emits both, as it emits `k:`.
The owner's note (DRIVE_FILE_NOTES_RFC §5.4, D7) reuses the existing families under `AboutField.OWNER_NOTE` (no new
family, so no KEYS_VERSION bump): its classifier names and words are `e:` (exact: the owner's own words), the words'
skeletons `ex:` / `exl:`, its numbers and dates `v:` / `y:` / `m:` by the profile's own recipe. The owner's doc type
and life areas override the profile's in the `l:` grouping keys, and the D31 names of an overridden field are left out.
"""
from __future__ import annotations

from functools import lru_cache

import hashlib
import os
import re
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_content_terms import (
    _AMOUNT, _DAY_MONTH_YEAR, _DMY, _DMY_SPACED, _GROUP_MIN_DIGITS, _GROUPS, _ID, _MIN_DIGITS, _MONTH, _MONTH_DAY_YEAR,
    _MONTH_FORMS, _MONTH_OF, _YMD, CONTENT_TERMS_PER_CHUNK, amount_or_date, capped_terms, clip_query, content_terms, query_term_spans,
    term_spans,
)
from .drive_enrichment import DriveProfile, Grade, GradedField, KeyValue, PhotoObject, ProfileEntity, is_unconfirmed
from .drive_index import TriageVerdict
from .drive_index_params import (
    ABOUT_KEYS_PER_RECORD, CONCEPT_KEYS_MAX, ENTITY_FORMS_MAX, MAX_CONCEPTS, NAME_MIN_TRIGRAMS, NAME_QUERY_KEYS_MAX,
    NOTE_KEYS_MAX,
    PHOTO_OBJECT_FORMS_MAX, PHOTO_OBJECTS_MAX, PHOTO_SCENE_LABELS_MAX, PHRASE_RUN_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX,
    PROFILE_KEY_VALUES_MAX, PROFILE_WORD_LIST_MAX, QUERY_TERM_CHARS, WORD_KEYS_PER_CHUNK,
)
from .drive_key_forms import (
    FORM_TABLES, SHORT_WORD_LETTERS, Token, is_short_function_word, primary, query_skeletons, scan, skeleton_key,
    token_forms, token_spans,
)
from .drive_key_sources import (
    AboutField, AboutSource, KeyedRecord, RecordKeyInputs, effective_doc_type, effective_life_areas,
)
from .drive_labels import (
    CAPTURE_NAMES, DOC_TYPE_NAMES, KIND_WORDS, LIFE_DOMAIN_NAMES, PHOTO_KIND_WORDS, SCENE_NAMES,
    capture_names_digest, label_names_digest, scene_names_digest,
)
from .drive_match import FAMILY_SOURCE_CLASS, Concept, KeyFamily, QueryConcepts, SourceClass
from .drive_name_keys import _is_cyrillic, _strip_accents, _trigrams

__all__ = [
    "FUNCTION_WORDS", "GENERIC_WORDS", "NAME_KEYS_V2", "TIME_WORDS", "about_keys_capped", "about_keys_of", "about_sources",
    "all_forms", "family_keys", "fetch_batches", "golden_digest", "index_tables_digest", "match_spans",
    "name_keys_v2", "name_match_v2", "name_word_concepts", "note_keys", "note_keys_digest", "phrase_keys", "query_concepts", "query_name_keys_v2", "source_class_of_about_key",
    "source_class_of_name_key", "source_class_of_word_key", "token_forms", "token_spans", "word_form_keys",
    "word_keys", "word_keys_capped",
]

NAME_KEYS_V2 = 2                 # the NAME_KEYS_VERSION `name_keys_v2` is made for; P5 sets it behind the gate
_W, _X, _N, _M, _Y = (KeyFamily.WORD.value, KeyFamily.TRANSLIT.value, KeyFamily.NUMBER.value, KeyFamily.MONTH.value,
                      KeyFamily.YEAR.value)
_E, _EX, _PE, _V, _K = (KeyFamily.ENTITY.value, KeyFamily.ENTITY_TRANSLIT.value, KeyFamily.PHOTO_ENTITY.value,
                        KeyFamily.VALUE.value, KeyFamily.KIND.value)
_PN = KeyFamily.PHOTO_NUMBER.value
_CP, _CPA = KeyFamily.CAPTURE.value, KeyFamily.CAPTURE_AUTO.value
_SL = KeyFamily.SCENE_LABEL.value
_VA, _YA, _MA = KeyFamily.VALUE_AUTO.value, KeyFamily.YEAR_AUTO.value, KeyFamily.MONTH_AUTO.value
_DETECTED = "detected"   # drive_capture.CaptureSource.DETECTED (the value the record stores)
_XL, _EXL, _EF = (KeyFamily.TRANSLIT_LATIN.value, KeyFamily.ENTITY_TRANSLIT_LATIN.value,
                  KeyFamily.ENTITY_FORM.value)
_LATIN_SKELETONS = frozenset({_XL, _EXL})
# The record word families a query word is sent under besides `e:`/`ex:`/`k:`/`pe:`/`v:` (§25.5.1 step 5).
_WORD_FAMILIES: Tuple[str, ...] = (KeyFamily.LABEL_WORD.value, KeyFamily.TOPIC_WORD.value,
                                   KeyFamily.DOMAIN_WORD.value, KeyFamily.VALIDITY_WORD.value,
                                   KeyFamily.PHOTO_WORD.value)
_GRADE_ORDER: Mapping[str, int] = {Grade.IN_TEXT.value: 0, Grade.IN_TEXT_AS.value: 1, Grade.NOT_IN_TEXT.value: 2}
_YEAR_MIN, _YEAR_MAX = 1900, 2099
_SHORT_ID_CHARS = 5   # a query id with letters this short ("ред3", "a4") is words, not one number (review m1)
# Person and place entities also key their last token alone (review m8: "Тарас Мельник" is about "Мельник").
_LAST_TOKEN_ENTITY_TYPES = frozenset({"person", "place"})

# -- query-only word lists (not hashed: they change no stored key; SHORT_FUNCTION_WORDS, which do, are) ----------
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
cómo cual cuál quien quién mi mis tu tus su sus me te se lo le les es son está están hay este esta estos estas
ese esa eso
le les une des du au aux à dans sur pour par avec et ou mais qui quoi où quand comment mon ma mes ton ta tes sa ses
je il elle nous vous ils elles ce cet cette ces est sont ne pas
этом этой этих этим этими этому цього цієї цих цим цими цьому цій
""".split()) - {"son"}   # "son" (fr possessive) is an English noun; "como" (es) is left out: Lake Como (review m4)
# Time expressions (en/es/uk/ru, with their case forms; P6 review M2): a time span belongs in the date fields (D25),
# and left in the words it must not become a concept that D29 blocks ("в этом году" -> "No file holds 'году'").
# Removed like generic words and named in the result's ignored list.
TIME_WORDS: FrozenSet[str] = frozenset("""
year years yearly month months week weeks day days today yesterday tomorrow tonight last next past recent recently
ago since current
год году года годы годов лет годах месяц месяца месяце месяцы месяцев неделя недели неделе неделю недель день дня
дней сегодня вчера завтра недавно прошлый прошлом прошлого прошлой прошлую прошлые прошлых последний последняя
последнее последние последних последней последнюю текущий текущем текущего этот эту
рік року роки років році місяць місяця місяці місяців місяці тиждень тижня тижні тижнів день дня днів сьогодні
вчора завтра нещодавно минулий минулого минулому минулої минулі минулих останній останнього останні останніх
останньої поточний поточного цей цю
año años mes meses semana semanas día días hoy ayer mañana pasado pasada pasados pasadas último última últimos
últimas próximo próxima reciente recientes recientemente actual
""".split())
GENERIC_WORDS: FrozenSet[str] = frozenset("""
all everything anything something every file files document documents doc docs stuff thing things info information
find search show look give get need want please regarding concerning
всё все весь вся всех всего нибудь файл файлы файла файлов документ документы документа документов найди найти
покажи показать поищи ищи информация информацию касается нужно нужны хочу пожалуйста
усе усі всі файли файлів документи документів знайди знайти пошукай шукай інформація інформацію потрібно будь
ласка
todo todos toda todas archivo archivos fichero ficheros documento documentos busca buscar encuentra encontrar
muestra mostrar información informacion
tout tous toute toutes fichier fichiers trouve trouver cherche chercher montre montrer
what which much many сколько скільки какой какая какое какие каких каким какую який яка яке які якого якої яких
якому cuánto cuánta cuántos cuántas cuanto cuanta cuantos cuantas qué cuál cuáles cuales
""".split())   # the last line: interrogative phrasing words (P7 eval), ignored and named like the generic ones

_TIME_PLAIN: FrozenSet[str] = frozenset(_strip_accents(w) for w in TIME_WORDS)   # "año" and "ano" alike (xc F2)

_MONTH_YEAR = re.compile(rf"(?<![\w]){_MONTH},?\s+(?:(?:de|del)\s+)?(\d{{4}})(?!\d)", re.IGNORECASE)

Span = Tuple[int, int]
_Item = Tuple[str, AboutSource]


def all_forms(token: str) -> Tuple[str, ...]:
    """The token's native forms (§25.2.4 after review B2: the index stores no transliterated spellings)."""
    return token_forms(token)


# -- families -------------------------------------------------------------------------------------------------------

Skel = Tuple[str, bool]    # a skeleton string, and whether it comes from Latin words only (`xl:` / `exl:`, xc F3)


def _latin_word(word: str) -> bool:
    """A word whose skeleton is a Latin word's (the index keys it `xl:`): no Cyrillic letter in it."""
    return not _is_cyrillic(word)


def _skel_key(word: str, cyr_prefix: str, lat_prefix: str) -> Optional[str]:
    """The index's skeleton key of one word, under the prefix of the script it came from (xc F3)."""
    sk = skeleton_key(word)
    if not sk:
        return None
    return (lat_prefix if _latin_word(word) else cyr_prefix) + sk


def _phrase(tokens: Sequence[str]) -> Tuple[List[str], List[Skel]]:
    """(native strings, skeletons) of a named thing: one token gives its forms and its skeleton; several join each
    token's form (b) — and each token's skeleton — with `_`. Articles are kept. A skeleton is Latin when every token
    is."""
    if not tokens:
        return [], []
    latin = all(_latin_word(t) for t in tokens)
    if len(tokens) == 1:
        sk = skeleton_key(tokens[0])
        return list(token_forms(tokens[0])), [(sk, latin)] if sk else []
    return ["_".join(primary(t) for t in tokens)], [("_".join(skeleton_key(t) or primary(t) for t in tokens), latin)]


def _phrase_items(text: str, last_token: bool = False) -> Tuple[List[str], List[Skel]]:
    """A spelling's phrase strings; a spelling that is one hyphen compound also gives the joined word's, and a
    person's or place's spelling of several words also its last word's (review m8)."""
    toks = scan(text)
    words = [t.text for t in toks]
    native, skel = _phrase(words)
    singles: List[str] = []
    if len(toks) > 1 and toks[0].compound and toks[0].compound_end == toks[-1].end:
        singles.append(toks[0].compound)
    if last_token and len(words) > 1:
        singles.append(words[-1])
    for single in singles:
        n, s = _phrase([single])
        native += n
        skel += s
    return list(dict.fromkeys(native)), list(dict.fromkeys(skel))


def _skel_keys(skels: Sequence[Skel]) -> List[str]:
    """Index-side entity skeleton keys: `exl:` for a Latin phrase, `ex:` otherwise."""
    return [(_EXL if latin else _EX) + sk for sk, latin in skels]


def phrase_keys(text: str) -> Tuple[str, ...]:
    """`e:` (native) and `ex:` / `exl:` (skeleton) keys of a named thing's spelling (§25.2.4)."""
    native, skel = _phrase_items(text)
    return tuple([_E + s for s in native] + _skel_keys(skel))


def _token_word_forms(token: Token) -> List[str]:
    """Native forms of a token, of its camelCase parts, and of the hyphen compound it starts (index side)."""
    forms = list(token_forms(token.text))
    for part in token.parts:
        forms += token_forms(part)
    if token.compound:
        forms += token_forms(token.compound)
    return forms


def family_keys(prefix: str, words: Iterable[str]) -> Tuple[str, ...]:
    """The native forms of every token of `words` under `prefix` (`lw:`, `tw:`, `dw:`, `lv:`, `pw:`), in order."""
    out: Dict[str, None] = {}
    for word in words:
        for token in scan(word):
            for form in _token_word_forms(token):
                out[prefix + form] = None
    return tuple(out)


def word_form_keys(text: str) -> Tuple[str, ...]:
    """`w:` keys of every token of `text` (a passage keyword's hint keys, §25.4.2)."""
    return family_keys(_W, [text])


# -- chunk keys and spans -------------------------------------------------------------------------------------------

def _year_ok(year: int) -> bool:
    return _YEAR_MIN <= year <= _YEAR_MAX


def _month_year_spans(text: str) -> List[Tuple[int, int, str]]:
    """Month-year spans ("March 2023", "березня 2023", "в марте 2023", "marzo de 2023") as (start, end, YYYY-MM)."""
    out = []
    for m in _MONTH_YEAR.finditer(text):
        year = int(m.group(2))
        if _year_ok(year):
            out.append((m.start(), m.end(), f"{year:04d}-{_MONTH_OF[m.group(1).casefold()]:02d}"))
    return out


def _text_items(text: str) -> List[Tuple[int, int, str]]:
    """Every `w:`, `x:`, `n:`, `m:` key of `text` with the span it came from, in text order."""
    items: List[Tuple[int, int, str]] = []
    for token in scan(text):
        items += [(token.start, token.end, _W + f) for f in _token_word_forms(token)]
        sk = _skel_key(token.text, _X, _XL)
        if sk:
            items.append((token.start, token.end, sk))
        if token.compound:
            sk = _skel_key(token.compound, _X, _XL)
            if sk:
                items.append((token.start, token.compound_end, sk))
    spans = term_spans(text)
    kept_terms = set(capped_terms({f for span in spans for f in span[2]}))   # the normaliser's per-chunk cap
    for start, end, forms, category in spans:
        items += [(start, end, _N + f) for f in forms if f in kept_terms]
        if category == "date":
            items += [(start, end, _M + f[:7]) for f in forms]
    items += [(start, end, _M + ym) for start, end, ym in _month_year_spans(text)]
    items.sort(key=lambda item: item[0])
    return items


def word_keys_capped(text: str) -> Tuple[Tuple[str, ...], bool]:
    """A chunk's keys and whether the cap cut some: past WORD_KEYS_PER_CHUNK the earliest text's keys are kept."""
    keys = list(dict.fromkeys(key for _, _, key in _text_items(text)))
    return tuple(keys[:WORD_KEYS_PER_CHUNK]), len(keys) > WORD_KEYS_PER_CHUNK


def word_keys(text: str) -> Tuple[str, ...]:
    """A chunk's `word_keys`: `w:`, `x:`, `n:`, `m:` of its text (§25.2.2)."""
    return word_keys_capped(text)[0]


def match_spans(text: str, keys: Sequence[str]) -> Tuple[Span, ...]:
    """The spans of `text` where any of `keys` (one concept's `w:`, `x:`, `n:` or `m:` keys) occurs, sorted and
    unique: the input of the proximity window (§25.5.4), from the same tokeniser as the keys."""
    wanted = set(keys)
    return tuple(sorted({(start, end) for start, end, key in _text_items(text) if key in wanted}))


# -- source classes -------------------------------------------------------------------------------------------------

def _prefix(key: str) -> str:
    return key[:key.index(":") + 1] if ":" in key else ""


_FAMILY_BY_PREFIX: Mapping[str, KeyFamily] = {f.value: f for f in KeyFamily}


def source_class_of_about_key(key: str) -> Optional[SourceClass]:
    """The class a record's about key proves, from its prefix alone; None for `l:` (never queried)."""
    family = _FAMILY_BY_PREFIX.get(_prefix(key))
    return FAMILY_SOURCE_CLASS.get(family) if family else None


def source_class_of_word_key(key: str) -> Optional[SourceClass]:
    """A chunk key: `w:`, `n:`, `m:` are passage words; `x:` / `xl:` (a skeleton, across scripts) a translation."""
    p = _prefix(key)
    if p in (_X, _XL):
        return SourceClass.TRANSLATION
    return SourceClass.PASSAGE_WORDS if p in (_W, _N, _M) else None


def source_class_of_name_key(key: str) -> Optional[SourceClass]:
    """A name key: `w:`, `n:`, `y:` are verified; `x:` / `xl:` a translation; trigrams (`t:`) never cover."""
    p = _prefix(key)
    if p in (_X, _XL):
        return SourceClass.TRANSLATION
    return SourceClass.VERIFIED_RECORD if p in (_W, _N, _Y) else None


# -- record keys ----------------------------------------------------------------------------------------------------

def _value_forms(text: str) -> List[str]:
    """A value in the form the query produces (review M3): the maximal number-term forms, else, for a value with no
    number term, its casefolded letters and digits."""
    forms = [f for span in query_term_spans(text) for f in span[2]]
    if forms:
        return forms
    plain = "".join(ch for ch in _strip_accents(text.casefold()) if ch.isalnum())
    return [plain] if plain else []


def _unspaced_id_forms(printed: str) -> List[str]:
    """An ID or digit group printed with separators (Δ91): its separator-free casefolded alphanumeric form, so
    `QX4471902` finds "QX 4471902"; and its alphabetic tokens ("qx"), which the digits' number term leaves out, so the
    spaced query's word `QX` is covered too. At least three characters and one digit; never an amount or a date
    (their separators carry meaning)."""
    plain = "".join(ch for ch in _strip_accents(printed.casefold()) if ch.isalnum())
    if len(plain) < 3 or not any(ch.isdigit() for ch in plain) or amount_or_date(printed):
        return []
    words = ["".join(ch for ch in _strip_accents(t.casefold()) if ch.isalnum()) for t in printed.split()]
    return [plain, *(w for w in words if len(w) >= 2 and w.isalpha())]


def _date_items(year: int, month: int, field: AboutField, detail: str, printed: str, *,
                held: bool = True) -> List[_Item]:
    """`y:` and `m:` keys of a date; `ya:` and `ma:` (automatic) of one the file's text does not hold (Δ60)."""
    source = AboutSource(field, detail, printed, SourceClass.VERIFIED_RECORD if held else SourceClass.AUTOMATIC_RECORD)
    return [(f"{_Y if held else _YA}{year:04d}", source), (f"{_M if held else _MA}{year:04d}-{month:02d}", source)]


def _grade_of(profile: DriveProfile, gfield: GradedField, value: str) -> Optional[str]:
    """The best grade the profile gave this value; None when it gave none (a profile stored before grading)."""
    grades = [g.grade for g in profile.graded if g.field == gfield.value and g.value == value]
    return min(grades, key=lambda g: _GRADE_ORDER.get(g, 0)) if grades else None


def _about_items(rec: KeyedRecord) -> List[_Item]:
    """Every about key with its source, earliest kept first under the cap: labels, kind, verified fields, then the
    automatic families."""
    items: List[_Item] = []

    def add(keys: Iterable[str], field: AboutField, detail: str, printed: str, in_file: str = "") -> None:
        for key in keys:
            items.append((key, AboutSource(field, detail, printed, source_class_of_about_key(key), in_file)))

    def add_phrase(prefix: str, text: str, field: AboutField, detail: str, last: bool = False, *,
                   skeletons: bool = False, in_file: str = "") -> None:
        native, skel = _phrase_items(text, last)
        add([prefix + s for s in native] + (_skel_keys(skel) if skeletons else []), field, detail, text, in_file)

    # the owner's note first (DRIVE_FILE_NOTES_RFC §5.4): the record cap keeps the earliest keys, so it never cuts them
    items.extend(_note_items(rec.owner_note_text, rec.owner_doc_type, rec.owner_life_areas)[0])
    profile = rec.profile
    lab = KeyFamily.LABEL.value
    # D7: the owner's doc type and life areas override the profile's in the grouping keys, also on a record with no
    # profile; without them the profile's are emitted exactly as before (a profile's "" type included)
    if rec.owner_doc_type:
        add([f"{lab}type:{rec.owner_doc_type}"], AboutField.LABEL, "type", rec.owner_doc_type)
    elif profile is not None:
        add([f"{lab}type:{profile.doc_type}"], AboutField.LABEL, "type", profile.doc_type)
    for domain in (rec.owner_life_areas if rec.owner_life_areas
                   else profile.life_domains if profile is not None else ()):
        add([f"{lab}domain:{domain}"], AboutField.LABEL, "domain", domain)
    if rec.valid_until is not None:
        add([f"{lab}valid"], AboutField.LABEL, "valid", rec.valid_until.isoformat()[:10])

    kind_words = list(KIND_WORDS.get(rec.kind, ()))
    if rec.kind == "image" and rec.triage_verdict is TriageVerdict.PHOTO:
        kind_words += PHOTO_KIND_WORDS
    for word in kind_words:
        add_phrase(_K, word, AboutField.KIND, rec.kind)
    if rec.capture and rec.triage_verdict is not TriageVerdict.PHOTO:
        # §26.3 (Δ57, Δ58): a photograph has no capture; `digital` has no words
        prefix = _CP if rec.capture_source == _DETECTED else _CPA
        for word in CAPTURE_NAMES.get(rec.capture, ()):
            add_phrase(prefix, word, AboutField.CAPTURE, rec.capture)

    if profile is not None:
        for entity in profile.entities[:PROFILE_ENTITIES_MAX]:
            last = entity.type in _LAST_TOKEN_ENTITY_TYPES
            # what the file prints: `printed` (graded `in_text`) and the spellings found; an entity the text holds only
            # written differently has no printed spelling of its own, and one it does not hold has none at all (Δ60)
            grade = _grade_of(profile, GradedField.ENTITY, entity.printed.strip() or entity.name)
            printed = dict.fromkeys(s for s in (entity.printed if grade in (None, Grade.IN_TEXT.value) else "",
                                                *(entity.found if grade != Grade.NOT_IN_TEXT.value else ())) if s)
            for spelling in dict.fromkeys((entity.name, entity.printed, *entity.forms[:ENTITY_FORMS_MAX])):
                if not spelling:
                    continue
                if spelling in printed:
                    # a spelling the file prints (verification found it): exact
                    add_phrase(_E, spelling, AboutField.ENTITY, entity.type, last, skeletons=True)
                else:
                    # a spelling the profiler wrote and the file does not print: the index's, never exact (xc F1)
                    add_phrase(_EF, spelling, AboutField.ENTITY_FORM, entity.type, last, skeletons=True,
                               in_file=entity.printed)
    for place in rec.place_forms:
        add_phrase(_E, place, AboutField.PLACE, "", skeletons=True)
    if profile is not None:
        for kv in profile.key_values[:PROFILE_KEY_VALUES_MAX]:
            forms = dict.fromkeys(_value_forms(kv.canonical) + _value_forms(kv.printed))
            held = not is_unconfirmed(profile, GradedField.VALUE, kv.printed.strip() or kv.canonical)
            if held:     # Δ91: "QX 4471902" is also found as the unspaced "QX4471902" (never for an unconfirmed value)
                forms.update(dict.fromkeys(_unspaced_id_forms(kv.printed)))
            add([(_V if held else _VA) + f for f in forms], AboutField.VALUE, kv.label, kv.printed)
        for d in profile.dates[:PROFILE_DATES_MAX]:
            iso = d.iso.isoformat()
            held = not is_unconfirmed(profile, GradedField.DATE, iso)
            add([(_V if held else _VA) + iso], AboutField.DATE, d.label, iso)
            items.extend(_date_items(d.iso.year, d.iso.month, AboutField.DATE, d.label, iso, held=held))
    if rec.effective_date is not None:
        eff = rec.effective_date
        items.extend(_date_items(eff.year, eff.month, AboutField.EFFECTIVE_DATE, "", eff.isoformat()[:10]))

    if profile is not None:
        # D31: the fixed names of the profile's own doc type and life areas join the model's words (after them); a field
        # the owner overrode has none (his names are `e:` keys already, DRIVE_FILE_NOTES_RFC §5.4)
        type_names = () if rec.owner_doc_type else DOC_TYPE_NAMES.get(profile.doc_type, ())
        domain_names = [] if rec.owner_life_areas else [n for d in profile.life_domains
                                                        for n in LIFE_DOMAIN_NAMES.get(d, ())]
        lists = [(KeyFamily.LABEL_WORD.value, [*profile.label_words[:PROFILE_WORD_LIST_MAX], *type_names],
                  AboutField.LABEL_WORD),
                 (KeyFamily.TOPIC_WORD.value, list(profile.topic_words[:PROFILE_WORD_LIST_MAX]),
                  AboutField.TOPIC_WORD),
                 (KeyFamily.DOMAIN_WORD.value, [*profile.domain_words[:PROFILE_WORD_LIST_MAX], *domain_names],
                  AboutField.DOMAIN_WORD)]
        if rec.valid_until is not None:
            lists.append((KeyFamily.VALIDITY_WORD.value, list(profile.validity_words[:PROFILE_WORD_LIST_MAX]),
                          AboutField.VALIDITY_WORD))
        for prefix, words, field in lists:
            for word in words:
                add(family_keys(prefix, [word]), field, "", word)

    if rec.kind == "image" and rec.triage_verdict is TriageVerdict.PHOTO:
        # D40: a photograph's scene labels, by their fixed names as phrases, like the `k:` table; a model said it
        for label in rec.photo_scene_labels[:PHOTO_SCENE_LABELS_MAX]:
            for word in SCENE_NAMES.get(label, ()):
                add_phrase(_SL, word, AboutField.SCENE_LABEL, label)

    objects = rec.photo_objects[:PHOTO_OBJECTS_MAX]
    for obj in objects:
        for spelling in dict.fromkeys((obj.name, *obj.forms[:PHOTO_OBJECT_FORMS_MAX])):
            add_phrase(_PE, spelling, AboutField.PHOTO_OBJECT, "")
    pw = KeyFamily.PHOTO_WORD.value
    for detail, text in (("description", rec.photo_description), ("scene", rec.photo_scene),
                         *(("object", obj.name) for obj in objects), ("visible_text", rec.visible_text)):
        if text:
            add(family_keys(pw, [text]), AboutField.PHOTO_WORD, detail, text)
    if rec.visible_text:
        for term in content_terms(rec.visible_text):
            add([_PN + term], AboutField.VISIBLE_TEXT_NUMBER, "", term)
    return items


# -- the owner's note (DRIVE_FILE_NOTES_RFC §5.4) -------------------------------------------------------------------

def _note_items(text: str, doc_type: str, life_areas: Sequence[str]) -> Tuple[List[_Item], bool]:
    """The owner note's about keys with their sources, in the order its cap keeps, and whether NOTE_KEYS_MAX cut some
    (§5.4, review I5, I6):
    1. the owner's classifier names (DOC_TYPE_NAMES, LIFE_DOMAIN_NAMES) as `e:` phrases, native forms only, no
       skeletons (as the D31 names), detail "type" / "area": at most ~85 keys, never cut;
    2. numbers and dates by the profile's own recipe, found as the query finds them (`query_term_spans`): a value's
       maximal forms as `v:` (never a fragment: "1 250,00" keys v:1250.00 and v:1250, not v:250) and an id's unspaced
       form; a year 1900-2099 also `y:`; a date `v:` ISO plus `y:` and `m:`; a month-year span `y:` and `m:YYYY-MM`.
       A short id with letters ("ред3") is left to the words, as the query leaves it (review m1);
    3. each content word (function, generic and time words removed, as the query removes them; a hyphen compound is
       one word) as its own phrase: `e:` native forms and its `ex:` / `exl:` skeleton.
    Past NOTE_KEYS_MAX distinct keys the tail is cut: only the last words' keys, the words staying in the card text."""
    items: List[_Item] = []
    note = AboutField.OWNER_NOTE

    def add(keys: Iterable[str], detail: str, printed: str) -> None:
        for key in keys:
            items.append((key, AboutSource(note, detail, printed, source_class_of_about_key(key))))

    type_names = DOC_TYPE_NAMES.get(doc_type, ()) if doc_type else ()
    area_names = [n for a in life_areas for n in LIFE_DOMAIN_NAMES.get(a, ())]
    for detail, names in (("type", type_names), ("area", area_names)):
        for name in names:
            native, _ = _phrase_items(name)
            add([_E + form for form in native], detail, name)
    text = text or ""
    if text:
        spans = query_term_spans(text)
        dates = [(s, e) for s, e, _, cat in spans if cat == "date"]
        months = [(s, e, ym) for s, e, ym in _month_year_spans(text) if not any(_overlaps((s, e), d) for d in dates)]
        consumed: List[Span] = []
        for start, end, forms, category in spans:
            printed = text[start:end].strip()
            if not forms:
                continue
            if category == "date":
                for iso in forms:
                    add([_V + iso], "date", iso)
                    try:
                        year, month = int(iso[:4]), int(iso[5:7])
                    except ValueError:
                        continue
                    items.extend(_date_items(year, month, note, "date", iso))
                consumed.append((start, end))
                continue
            if any(_overlaps((start, end), (ms, me)) for ms, me, _ in months):
                consumed.append((start, end))   # a year inside "March 2024": the month-year keys it
                continue
            if category == "id" and any(ch.isalpha() for ch in forms[0]) and len(forms[0]) <= _SHORT_ID_CHARS:
                continue   # "ред3": words, as the query reads it (review m1)
            values = dict.fromkeys([*forms, *(_unspaced_id_forms(printed) if category == "id" else [])])
            add([_V + f for f in values], "number", printed)
            add([_Y + f for f in forms if len(f) == 4 and f.isdigit() and _year_ok(int(f))], "number", printed)
            consumed.append((start, end))
        for start, end, ym in months:
            items.extend(_date_items(int(ym[:4]), int(ym[5:]), note, "date", text[start:end].strip()))
            consumed.append((start, end))
        toks = scan(text)
        i = 0
        while i < len(toks):   # the query's own word loop (`query_concepts`), so a word keys as a query word reads
            first, j = i, i
            if toks[i].compound:
                while j + 1 < len(toks) and toks[j].end < toks[i].compound_end:
                    j += 1
            if toks[i].compound_kind == "alnum" and all(token_forms(t.text) for t in toks[first:j + 1]):
                j = first
            word = toks[i].compound if j > first else toks[i].text
            span = (toks[i].start, toks[j].end)
            i = j + 1
            if any(_overlaps(span, c) for c in consumed) or _is_removable(word) or not token_forms(word):
                continue
            native, skel = _phrase_items(word)
            add([_E + form for form in native] + _skel_keys(skel), "word", text[span[0]:span[1]])   # as written
    kept: Dict[str, None] = {}
    out: List[_Item] = []
    cut = False
    for key, source in items:
        if key not in kept:
            if len(kept) >= NOTE_KEYS_MAX:
                cut = True
                continue
            kept[key] = None
        out.append((key, source))
    return out, cut


def note_keys(text: str, doc_type: str = "", life_areas: Sequence[str] = ()) -> Tuple[str, ...]:
    """The owner note's keys (§5.4): classifier names first, then numbers and dates, then words, at most NOTE_KEYS_MAX.
    The note store's `keys` and a noted record's leading about keys."""
    return tuple(dict.fromkeys(key for key, _ in _note_items(text, doc_type, life_areas)[0]))


def about_keys_capped(rec: KeyedRecord) -> Tuple[Tuple[str, ...], bool]:
    """The record's about keys and whether a cap cut some: ABOUT_KEYS_PER_RECORD (the earliest entries are kept), or
    the note's own NOTE_KEYS_MAX (its last words, DRIVE_FILE_NOTES_RFC §5.4)."""
    keys = list(dict.fromkeys(key for key, _ in _about_items(rec)))
    note_cut = bool(rec.owner_note_text) and _note_items(rec.owner_note_text, rec.owner_doc_type,
                                                          rec.owner_life_areas)[1]
    return tuple(keys[:ABOUT_KEYS_PER_RECORD]), len(keys) > ABOUT_KEYS_PER_RECORD or note_cut


def about_keys_of(rec: KeyedRecord) -> Tuple[str, ...]:
    """`about_keys` (§25.2.4): `e:`, `ef:`, `ex:`, `exl:`, `v:`, `y:`, `m:`, `k:`, `cp:`, `cpa:`, `sl:`, `lw:`, `tw:`, `dw:`,
    `lv:`, `pw:`, `pe:`, `pn:`, `l:`."""
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

def fetch_batches(keys: Sequence[str]) -> Tuple[Tuple[str, ...], ...]:
    """A concept's key list cut, in order, into fetches of at most CONCEPT_KEYS_MAX keys (Firestore's
    `array-contains-any` limit). Coverage is tested on the whole list, so a word's coverage never depends on its
    neighbours (review B1)."""
    return tuple(tuple(keys[i:i + CONCEPT_KEYS_MAX]) for i in range(0, len(keys), CONCEPT_KEYS_MAX))


def _is_function(token: str) -> bool:
    return is_short_function_word(token) or token in FUNCTION_WORDS or _strip_accents(token) in FUNCTION_WORDS


def _is_generic(token: str) -> bool:
    return token in GENERIC_WORDS or _strip_accents(token) in GENERIC_WORDS


def _is_removable(token: str) -> bool:
    return _is_function(token) or _is_generic(token) or _strip_accents(token) in _TIME_PLAIN


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _number_concept(word: str, forms: Sequence[str]) -> Concept:
    about: List[str] = []
    for f in forms:
        about += [_N + f, _V + f, _VA + f]
    about += [fam + f for f in forms if len(f) == 4 and f.isdigit() and _year_ok(int(f)) for fam in (_Y, _YA)]
    about += [_PN + f for f in forms]
    return Concept(word=word, about_keys=tuple(dict.fromkeys(about)), word_keys=tuple(_N + f for f in forms),
                   hint_keys=(), is_number=True)


def _numbers(text: str) -> Tuple[List[Concept], List[Span]]:
    """Number and month concepts in text order, and the stretches they consume (§25.5.1 step 3): a date claims
    first, a month name with a year next, then amounts, digit groups and ids outside both. A short id with letters
    ("ред3") is left to the word concepts of its tokens (review m1)."""
    spans = query_term_spans(text)
    dates = [(s, e) for s, e, _, cat in spans if cat == "date"]
    months = [(s, e, ym) for s, e, ym in _month_year_spans(text) if not any(_overlaps((s, e), d) for d in dates)]
    found: List[Tuple[int, int, Concept]] = []
    for s, e, forms, cat in spans:
        if not forms or (cat != "date" and any(_overlaps((s, e), (ms, me)) for ms, me, _ in months)):
            continue
        if cat == "id" and any(ch.isalpha() for ch in forms[0]) and len(forms[0]) <= _SHORT_ID_CHARS:
            continue
        found.append((s, e, _number_concept(text[s:e].strip().casefold(), forms)))
    for s, e, ym in months:
        key = (_M + ym,)
        found.append((s, e, Concept(word=text[s:e].strip().casefold(), about_keys=(*key, _MA + ym), word_keys=key,
                                    hint_keys=(), is_number=True)))
    found.sort(key=lambda f: f[0])
    seen: Set[Tuple[str, ...]] = set()
    concepts: List[Concept] = []
    for _, _, concept in found:
        if concept.word_keys not in seen:
            seen.add(concept.word_keys)
            concepts.append(concept)
    return concepts, [(s, e) for s, e, _ in found]


def _runs(words: Sequence[str], i: int, j: int) -> Tuple[List[str], List[Skel]]:
    """Native and skeleton phrase strings of the runs of 2..PHRASE_RUN_MAX tokens holding tokens i..j (a compound
    spans i..j), shorter first; a run ending in a function word names nothing and is skipped."""
    native: List[str] = []
    skel: List[Skel] = []
    for length in range(max(2, j - i + 1), PHRASE_RUN_MAX + 1):
        for start in range(j - length + 1, i + 1):
            end = start + length
            if start < 0 or end > len(words) or _is_function(words[end - 1]):
                continue
            n, s = _phrase(words[start:end])
            native += n
            skel += s
    return native, skel


def _query_skel_keys(skels: Sequence[str], latin: bool, cyr_prefix: str, lat_prefix: str) -> List[str]:
    """A query word's skeleton keys: a Latin word reads only Cyrillic words' skeletons (it meets a Latin word through
    its `w:` forms, never through a Cyrillic stem: xc F3); a Cyrillic word reads both."""
    prefixes = (cyr_prefix,) if latin else (cyr_prefix, lat_prefix)
    return [p + s for s in skels for p in prefixes]


@lru_cache(maxsize=1)
def _capture_suffixes() -> FrozenSet[str]:
    """The keys CAPTURE_NAMES are stored under, without their `cp:` prefix: the index side run over every name."""
    suffixes: Set[str] = set()
    for capture in CAPTURE_NAMES:
        rec = RecordKeyInputs(kind="pdf", capture=capture, capture_source="detected")
        suffixes.update(k[len(_CP):] for k in about_keys_of(rec) if k.startswith(_CP))
    return frozenset(suffixes)


@lru_cache(maxsize=1)
def _scene_suffixes() -> FrozenSet[str]:
    """The keys SCENE_NAMES are stored under, without their `sl:` prefix: the index side run over every label."""
    suffixes: Set[str] = set()
    for label in SCENE_NAMES:
        rec = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, photo_scene_labels=(label,))
        suffixes.update(k[len(_SL):] for k in about_keys_of(rec) if k.startswith(_SL))
    return frozenset(suffixes)


def _word_concept(word: str, run_native: Sequence[str], run_skel: Sequence[Skel]) -> Concept:
    """§25.5.1 step 5, uncut: `w:` native forms and `x:` (and, for a Cyrillic word, `xl:`) skeletons for chunks; `w:`
    native forms for hints; for records `e:` and `ef:` forms and runs, `ex:` (`exl:`) skeletons and runs, `k:` forms
    and runs, `cp:` and `cpa:` forms and runs (§26.3), `sl:` forms and runs (D40), the word families, `pe:` forms and runs, and `v:` of the word as typed (a value without digits, review
    M3)."""
    own = token_forms(word)
    latin = _latin_word(word)
    skels = query_skeletons(word)
    about = [_E + f for f in own] + [_E + r for r in run_native]
    about += [_EF + f for f in own] + [_EF + r for r in run_native]
    about += _query_skel_keys(skels, latin, _EX, _EXL)
    for run, run_latin in run_skel:
        about += _query_skel_keys([run], run_latin, _EX, _EXL)
    about += [_K + f for f in own] + [_K + r for r in run_native]
    stored = _capture_suffixes()   # only forms a capture name can be stored under: no cp/cpa key for other words
    about += [fam + k for fam in (_CP, _CPA) for k in [*own, *run_native] if k in stored]
    scenes = _scene_suffixes()   # likewise: no `sl:` key for a word no scene name is stored under (D40)
    about += [_SL + k for k in [*own, *run_native] if k in scenes]
    about += [fam + f for fam in _WORD_FAMILIES for f in own]
    about += [_PE + f for f in own] + [_PE + r for r in run_native]
    plain = "".join(ch for ch in _strip_accents(word) if ch.isalnum())
    if plain:
        about += [_V + plain, _VA + plain]
    chunk = tuple(dict.fromkeys([_W + f for f in own] + _query_skel_keys(skels, latin, _X, _XL)))
    return Concept(word=word, about_keys=tuple(dict.fromkeys(about)), word_keys=chunk,
                   hint_keys=tuple(_W + f for f in own), is_number=False)


def query_concepts(drive_query: str) -> QueryConcepts:
    """`drive_query` as strict-AND concepts (§25.5.1): numbers first, then words in query order, at most
    MAX_CONCEPTS. Function and generic words, and 1-letter words, are `ignored`; concepts past the cap are `cut`.
    Phrase runs are built over every token before anything is removed; a hyphen compound ("Wi-Fi") is one word. The
    key lists are uncut: fetch them with `fetch_batches`."""
    text = clip_query(drive_query, QUERY_TERM_CHARS)
    numbers, consumed = _numbers(text)
    toks = scan(text)
    words = [t.text for t in toks]
    ignored: List[str] = []
    concepts: List[Concept] = []
    seen: Set[str] = set()
    i = 0
    while i < len(toks):
        first, j = i, i
        if toks[i].compound:
            while j + 1 < len(toks) and toks[j].end < toks[i].compound_end:
                j += 1
        if toks[i].compound_kind == "alnum" and all(token_forms(t.text) for t in toks[first:j + 1]):
            j = first        # "ред3": its words, each with keys, so "ред. 3" is found too (review m1)
        word = toks[i].compound if j > first else toks[i].text
        span = (toks[i].start, toks[j].end)
        i = j + 1
        if any(_overlaps(span, c) for c in consumed):
            continue
        if _is_removable(word) or not token_forms(word):
            if word not in ignored:
                ignored.append(word)
            continue
        if word not in seen:
            seen.add(word)
            concepts.append(_word_concept(word, *_runs(words, first, j)))
    ordered = numbers + concepts
    return QueryConcepts(concepts=tuple(ordered[:MAX_CONCEPTS]), ignored=tuple(ignored),
                         cut=tuple(c.word for c in ordered[MAX_CONCEPTS:]))


# -- name keys v2 ---------------------------------------------------------------------------------------------------

def name_keys_v2(name: str) -> Tuple[str, ...]:
    """Name keys v2 (§25.2.4, NAME_KEYS_VERSION 2): every token's native `w:` forms (with camelCase parts and hyphen
    compounds), one skeleton per word (`x:` for a Cyrillic word, `xl:` for a Latin one, xc F3), the `n:`/`y:` forms of
    digit tokens and of the name's number terms (with and without its extension), and trigrams (`t:`) of each token
    and skeleton, which never cover. A name whose stem holds no keyed word, only 2-letter function words ("My.docx",
    "Où.txt"), keys those words exactly, so a query of that word alone still finds it (xc F2)."""
    keys: Set[str] = set()
    for token in scan(name):
        keys.update(_W + f for f in _token_word_forms(token))
        for word in (token.text, token.compound):
            sk = skeleton_key(word) if word else None
            if sk:
                keys.add((_XL if _latin_word(word) else _X) + sk)
                keys.update("t:" + g for g in _trigrams(sk))
        keys.update("t:" + g for g in _trigrams(_strip_accents(token.text)))
        if token.text.isdigit() and len(token.text) >= _MIN_DIGITS:
            keys.add(_N + token.text)
            if len(token.text) == 4 and _year_ok(int(token.text)):
                keys.add(_Y + token.text)
    stem = os.path.splitext(name)[0]
    keys.update(_N + t for t in (*content_terms(name), *content_terms(stem)))
    stem_tokens = scan(stem)
    if not any(_token_word_forms(t) for t in stem_tokens):
        keys.update(_W + _short_form(t.text) for t in stem_tokens if _letters(t.text) == SHORT_WORD_LETTERS)
    return tuple(sorted(keys))


def _letters(word: str) -> int:
    return sum(ch.isalpha() for ch in word)


def _short_form(word: str) -> str:
    """A 2-letter word's exact key form: accents stripped from a Latin word (as `token_forms` does)."""
    plain = _strip_accents(word)
    return plain if _latin_word(plain) else word


def name_word_concepts(query: str) -> Tuple[Concept, ...]:
    """A query left with no concept, as name-word concepts searched by the name stream alone (P6 review m6, xc F2):
    its generic words ("todo", "files") when it holds any, else every ignored word of 2 letters or more (a function or
    time word: "My.docx", "Mañana.docx", the band "The The"). Each concept holds the name keys a name of that word has
    (`w:`, `n:`, `y:`) and the word's query skeletons (`x:`, and `xl:` for a Cyrillic word); no trigram."""
    ignored = [w for w in query_concepts(query).ignored if _letters(w) >= SHORT_WORD_LETTERS]
    generic = [w for w in ignored if _is_generic(w)]
    out = []
    for word in generic or ignored:
        whole = [k for k in name_keys_v2(word) if k.startswith((_W, _N, _Y))]
        skels = _query_skel_keys(query_skeletons(word), _latin_word(word), _X, _XL)
        keys = tuple(dict.fromkeys(whole + skels))
        if keys:
            out.append(Concept(word=word, about_keys=(), word_keys=keys, hint_keys=(), is_number=False))
    return tuple(out)


def query_name_keys_v2(query: str) -> Tuple[str, ...]:
    """The name stream's query keys over name keys v2: the concepts' `w:`, `x:` and `n:` keys, then the words'
    trigrams, cut at NAME_QUERY_KEYS_MAX (whole keys first)."""
    q = query_concepts(query)
    whole = [k for c in q.concepts for k in c.word_keys]
    grams = ["t:" + g for c in q.concepts if not c.is_number for g in _trigrams(_strip_accents(c.word))]
    return tuple(dict.fromkeys(whole + grams))[:NAME_QUERY_KEYS_MAX]


def name_match_v2(record_keys: Sequence[str], query_keys: Sequence[str]) -> Optional[int]:
    """Shared name keys v2, or None below the floor (§10.1, §25.2.4): one whole key — `w:`, `x:`, `n:` or `y:`, the
    forms the index writes for a word, its skeleton or its digits — or NAME_MIN_TRIGRAMS trigrams (`t:`), which never
    cover on their own. The v1 `drive_name_keys.name_match` counted only `w:` as whole: on v2 keys a number or a
    skeleton matched alone fell under the floor (review rv_sv2_p5 M2)."""
    shared = set(record_keys) & set(query_keys)
    grams = sum(1 for k in shared if k.startswith("t:"))
    if grams == len(shared) and grams < NAME_MIN_TRIGRAMS:
        return None
    return len(shared)


# -- the version pins -----------------------------------------------------------------------------------------------

def index_tables_digest() -> str:
    """SHA-256 over every table and constant an index key depends on (forms, skeleton, transliteration, kind words,
    families, the number-term normaliser's patterns and caps). A test pins it per (KEYS_VERSION, NAME_KEYS_V2)."""
    tables = (
        FORM_TABLES, (_YEAR_MIN, _YEAR_MAX, _SHORT_ID_CHARS), sorted(_LAST_TOKEN_ENTITY_TYPES),
        (PROFILE_ENTITIES_MAX, ENTITY_FORMS_MAX, PROFILE_KEY_VALUES_MAX, PROFILE_DATES_MAX, PROFILE_WORD_LIST_MAX,
         PHOTO_OBJECTS_MAX, PHOTO_OBJECT_FORMS_MAX, PHOTO_SCENE_LABELS_MAX, WORD_KEYS_PER_CHUNK, ABOUT_KEYS_PER_RECORD),
        sorted((k, v) for k, v in KIND_WORDS.items()), PHOTO_KIND_WORDS, [f.value for f in KeyFamily],
        label_names_digest(), capture_names_digest(), scene_names_digest(),
        sorted(_MONTH_FORMS.items()), _MONTH_YEAR.pattern, CONTENT_TERMS_PER_CHUNK, _MIN_DIGITS, _GROUP_MIN_DIGITS,
        _AMOUNT.pattern, _DMY.pattern, _YMD.pattern, _ID.pattern, _GROUPS.pattern, _DAY_MONTH_YEAR.pattern,
        _MONTH_DAY_YEAR.pattern, _DMY_SPACED.pattern,
    )
    return hashlib.sha256(repr(tables).encode("utf-8")).hexdigest()


# A fixed multilingual fixture whose keys are pinned too (review m9): a change to a rule's code, not only to a table,
# fails the pin until KEYS_VERSION moves.
_GOLDEN_TEXTS: Tuple[str, ...] = (
    "Договор аренды квартиры в Лиссабоне от 15.03.2024, сумма 1 250,00 EUR, депозит, подписи сторон.",
    "Квитки до Одеси та Харкова: у Львові о 18:30, комп'ютер, сім'я, iPhone 15, Wi-Fi, ФОП Мельник.",
    "Houses, policies, invoices and receipts for the Lisbon lease; PayPal; e-mail; ред3; March 2024.",
    "медицинские записи по прививкам, нові квартири, нової картки, ёлка, за́мок, квар-\nтира",
)


def golden_digest() -> str:
    """SHA-256 over the keyer's output on `_GOLDEN_TEXTS` and one rich record. A test pins it per KEYS_VERSION."""
    out: List[object] = []
    for text in _GOLDEN_TEXTS:
        out += [word_keys(text), name_keys_v2(text[:60]), query_concepts(text)]
    rec = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, place_forms=("Porto", "Порту"),
                          photo_objects=(PhotoObject("Torre dos Clerigos"),), visible_text="Gate 12345",
                          profile=DriveProfile("t", "w", "ru", "receipt", entities=(
                              ProfileEntity("Тарас Мельник", "person", "Тарас Мельник"),),
                              key_values=(KeyValue("invoice_number", "INV-2024/0012", "INV-2024/0012"),),
                              label_words=("receipt", "квитанции")))
    out.append(about_keys_of(rec))
    return hashlib.sha256(repr(out).encode("utf-8")).hexdigest()


# Fixed owner notes in four languages (DRIVE_FILE_NOTES_RFC §5.4): their keys, and a noted record's about keys and
# grouping overrides, are pinned per NOTE_MERGE_VERSION by their own test. Kept out of the two digests above on purpose:
# a change here shapes only noted records, which a NOTE_MERGE_VERSION bump re-merges (§5.5, N9).
_NOTE_GOLDEN: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("Типовой договор подряда (образец): сумма 1.048,45 EUR, оплата до 30.10.2026", "contract", ("home", "finance")),
    ("Дозвіл на садовий сарай, рішення отримано: жовтень 2026", "", ("home",)),
    ("The hardware shop's invoice FA-0137 for the front-door lock, September 2026; Wi-Fi router ред3", "invoice", ()),
    ("Factura de Pinturas Exemplo por la pintura, 680,00 EUR, año 2026", "", ("finance", "home", "family")),
    ("", "contract", ()),
)


def note_keys_digest() -> str:
    """SHA-256 over the note keyer's output on `_NOTE_GOLDEN`, and over one noted record's about keys (a profile typed
    `invoice` overridden by the owner's `contract`). A test pins it per NOTE_MERGE_VERSION."""
    out: List[object] = [note_keys(text, doc_type, areas) for text, doc_type, areas in _NOTE_GOLDEN]
    rec = RecordKeyInputs(kind="pdf", owner_note_text=_NOTE_GOLDEN[0][0], owner_doc_type="contract",
                          owner_life_areas=("home",),
                          profile=DriveProfile("t", "w", "ru", "invoice", life_domains=("finance",),
                                               label_words=("invoice", "счёт")))
    out.append(about_keys_of(rec))
    out.append((effective_doc_type(rec), effective_life_areas(rec)))
    return hashlib.sha256(repr(out).encode("utf-8")).hexdigest()

