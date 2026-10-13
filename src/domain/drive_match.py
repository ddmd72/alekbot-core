"""
Drive match vocabulary — key families, source classes, tiers and query concepts of Search v2
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.4, §25.5.1, §25.5.4, frozen in §25.13.2). Pure data.

The functions that build keys and concepts (`drive_keys`, P1) and decide tiers (`drive_tiers`, P6) are written
against these names.

Public interface:
    KeyFamily                   the key prefixes (§25.2.4)
    SourceClass                 what a covering key proves (§25.2.4 "Source classes")
    MatchTier                   the result tiers, best first (§25.5.4)
    Concept, QueryConcepts      `drive_keys.query_concepts(drive_query)` (§25.5.1)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class KeyFamily(str, Enum):
    """Every key is a short string with one of these prefixes (§25.2.4)."""
    WORD = "w:"            # the forms of a token
    NUMBER = "n:"          # number terms (the content-terms normaliser)
    MONTH = "m:"           # m:YYYY-MM
    YEAR = "y:"            # y:YYYY
    ENTITY = "e:"          # entity phrases; photo place forms; photo object names
    VALUE = "v:"           # a verified key value's canonical form; a verified date's ISO form
    KIND = "k:"            # the kind words (drive_labels.KIND_WORDS)
    LABEL_WORD = "lw:"     # profile label_words
    TOPIC_WORD = "tw:"     # profile topic_words
    DOMAIN_WORD = "dw:"    # profile domain_words
    VALIDITY_WORD = "lv:"  # profile validity_words, only when valid_until is set
    PHOTO_WORD = "pw:"     # photo description, scene, objects and visible text
    LABEL = "l:"           # l:type:<doc_type>, l:domain:<domain>, l:valid — stored for grouping, never queried


class SourceClass(str, Enum):
    """The class of key that covers a concept in a file; a file's tier is set by the weakest class it needs (§25.5.4).
    Verified record: name keys, `e:` (photo objects excepted), `v:`, `y:`, `m:`, `k:`. Passage words: `word_keys`.
    Translation: `hint_keys`. Automatic record: `lw:`, `tw:`, `dw:`, `lv:`, `pw:` and photo-object `e:`."""
    VERIFIED_RECORD = "verified_record"
    PASSAGE_WORDS = "passage_words"
    TRANSLATION = "translation"
    AUTOMATIC_RECORD = "automatic_record"


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
    `k:` and the automatic families, plus number and date families for a number), `word_keys` for chunk `word_keys`,
    `hint_keys` for hint documents. Each list holds at most `CONCEPT_KEYS_MAX` keys."""
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
