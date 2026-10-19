"""
Drive match vocabulary — key families, source classes, tiers and query concepts of Search v2
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4, §25.5.1, §25.5.4, frozen in §25.13.2). Pure data.

The functions that build keys and concepts (`drive_keys`, P1) and decide tiers (`drive_tiers`, P6) are written
against these names. Several public classes in one module, by decision: one vocabulary, read together (CLAUDE.md's
one-class rule is waived here on purpose).

Public interface:
    KeyFamily                   the key prefixes (§25.2.4)
    FAMILY_SOURCE_CLASS         about-key family -> SourceClass (name keys are verified, word keys passage words,
                                hint keys translation: by the field that holds them, not by prefix)
    SourceClass                 what a covering key proves (§25.2.4 "Source classes")
    MatchTier                   the result tiers, best first (§25.5.4)
    Concept, QueryConcepts      `drive_keys.query_concepts(drive_query)` (§25.5.1)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Tuple


class KeyFamily(str, Enum):
    """Every key is a short string with one of these prefixes (§25.2.4)."""
    WORD = "w:"            # the native forms of a token (chunks, names): exact
    TRANSLIT = "x:"        # the skeleton of a Cyrillic word (chunks, names): a translation (review M8)
    TRANSLIT_LATIN = "xl:"  # the skeleton of a Latin word (chunks, names): a translation; a Latin query word never
                            # reads it, so two Latin words never meet through a skeleton's stem (xc F3: Mario/Maria)
    NUMBER = "n:"          # number terms (the content-terms normaliser)
    MONTH = "m:"           # m:YYYY-MM
    YEAR = "y:"            # y:YYYY
    ENTITY = "e:"          # entity spellings the file prints (verified) and the photo place's forms: exact
    ENTITY_FORM = "ef:"    # an entity spelling the profiler wrote that the file does not print: a translation (F1)
    ENTITY_TRANSLIT = "ex:"  # the skeleton of an entity phrase of Cyrillic words: a translation (review M8)
    ENTITY_TRANSLIT_LATIN = "exl:"  # ... of Latin words: a Latin query word never reads it (xc F3)
    PHOTO_ENTITY = "pe:"   # photo object names, keyed like `e:` phrases: automatic (review M3, RFC §25.2.4)
    VALUE = "v:"           # a verified key value's canonical form; a verified date's ISO form
    KIND = "k:"            # the kind words (drive_labels.KIND_WORDS)
    LABEL_WORD = "lw:"     # profile label_words
    TOPIC_WORD = "tw:"     # profile topic_words
    DOMAIN_WORD = "dw:"    # profile domain_words
    VALIDITY_WORD = "lv:"  # profile validity_words, only when valid_until is set
    PHOTO_WORD = "pw:"     # photo description, scene, objects and visible text
    PHOTO_NUMBER = "pn:"   # number terms of a photo's visible text: automatic (P1 review, M3 of Part 0)
    LABEL = "l:"           # l:type:<doc_type>, l:domain:<domain>, l:valid — stored for grouping, never queried
    CAPTURE = "cp:"        # the capture's names (drive_labels.CAPTURE_NAMES) when code detected it: verified (§26.3)
    CAPTURE_AUTO = "cpa:"  # ... when a model said it: automatic (Δ58)


class SourceClass(str, Enum):
    """The class of key that covers a concept in a file; a file's tier is set by the weakest class it needs (§25.5.4).
    Verified record: name keys (`w:`, `n:`, `y:`), `e:`, `v:`, `y:`, `m:`, `k:`, `cp:`. Passage words: `word_keys` `w:`,
    `n:`, `m:`. Translation: `hint_keys`, every transliteration skeleton (`x:`/`xl:` in chunks and names, `ex:`/`exl:`
    in records: a match across scripts is never exact, review M8), and an entity spelling the file does not print
    (`ef:`, xc F1). Automatic record: `lw:`, `tw:`, `dw:`, `lv:`, `pw:`,
    `pe:`, `pn:`, `cpa:`. An about key's class follows from its prefix alone, so
    tiers are decided on lean records."""
    VERIFIED_RECORD = "verified_record"
    PASSAGE_WORDS = "passage_words"
    TRANSLATION = "translation"
    AUTOMATIC_RECORD = "automatic_record"


# The class of a key in a record's `about_keys`, by family (§25.2.4). `l:` keys are never queried: no class.
FAMILY_SOURCE_CLASS: Mapping[KeyFamily, SourceClass] = {
    KeyFamily.ENTITY: SourceClass.VERIFIED_RECORD, KeyFamily.VALUE: SourceClass.VERIFIED_RECORD,
    KeyFamily.YEAR: SourceClass.VERIFIED_RECORD, KeyFamily.MONTH: SourceClass.VERIFIED_RECORD,
    KeyFamily.KIND: SourceClass.VERIFIED_RECORD, KeyFamily.CAPTURE: SourceClass.VERIFIED_RECORD,
    KeyFamily.LABEL_WORD: SourceClass.AUTOMATIC_RECORD, KeyFamily.TOPIC_WORD: SourceClass.AUTOMATIC_RECORD,
    KeyFamily.DOMAIN_WORD: SourceClass.AUTOMATIC_RECORD, KeyFamily.VALIDITY_WORD: SourceClass.AUTOMATIC_RECORD,
    KeyFamily.PHOTO_WORD: SourceClass.AUTOMATIC_RECORD, KeyFamily.PHOTO_ENTITY: SourceClass.AUTOMATIC_RECORD,
    KeyFamily.PHOTO_NUMBER: SourceClass.AUTOMATIC_RECORD, KeyFamily.CAPTURE_AUTO: SourceClass.AUTOMATIC_RECORD,
    KeyFamily.ENTITY_TRANSLIT: SourceClass.TRANSLATION,
    KeyFamily.ENTITY_TRANSLIT_LATIN: SourceClass.TRANSLATION, KeyFamily.ENTITY_FORM: SourceClass.TRANSLATION,
}


class MatchTier(str, Enum):
    """How a result matched, from most to least certain (P2, §25.5.4); each file is shown once, in its best tier."""
    ABOUT = "about"                # exact
    MENTIONED = "mentioned"        # exact
    TRANSLATED = "translated"      # automatic
    LABELLED = "labelled"          # automatic
    RELATED = "related"            # by meaning only, unverified; at most RELATED_MAX lines


@dataclass(frozen=True)
class Concept:
    """One query concept and the keys it emits per stream (§25.5.1 step 5): `about_keys` for the record stream (`e:`,
    `ex:`, `pe:`, `k:`, `v:` and the automatic families, plus number and date families for a number), `word_keys` for
    chunk `word_keys` (`w:`, `x:`, or `n:`/`m:`), `hint_keys` for hint documents. The lists are UNCUT: coverage is
    tested on the whole list, and the fetch splits each into lists of at most `CONCEPT_KEYS_MAX`
    (`drive_keys.fetch_batches`), so a word's coverage never depends on its neighbours (P1 review B1)."""
    word: str
    about_keys: Tuple[str, ...]
    word_keys: Tuple[str, ...]
    hint_keys: Tuple[str, ...]
    is_number: bool


@dataclass(frozen=True)
class QueryConcepts:
    """At most `MAX_CONCEPTS` concepts, numbers first then words in query order; `ignored` are the function and
    generic words removed, `cut` the words past the cap. Both are named in the result's searched line."""
    concepts: Tuple[Concept, ...]
    ignored: Tuple[str, ...]
    cut: Tuple[str, ...]
