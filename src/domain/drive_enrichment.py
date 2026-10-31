"""
Drive enrichment — index-time model output and its bookkeeping in Search v2
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2, §25.4, frozen in §25.13.2). Pure data: no I/O, no clock.

A profile describes a text file once (§25.4.1); passage hints are doc2query keywords and questions per chunk, each with
a verbatim evidence quote (§25.4.2); a photo description is enriched triage (§25.4.3). Every model field is either
verified against the text or shown as automatic (P5). Labels take values from `drive_labels`; states and labels are
plain strings on the record (§25.13.2), the enums below name the allowed values.

Several public classes in one module, by decision: the frozen core groups its value types by subject, as
`drive_index` and `drive_extraction` do, so a part reads its whole contract in one file (CLAUDE.md's one-class rule
is waived here on purpose).

Public interface:
    JobState, HoldReason, ProfileSource, HintType, UsageClass, EnrichJob
    ProfileDate, ProfileEntity, KeyValue, DriveProfile
    Grade, GradedField, GradedValue, is_unconfirmed       (evidence grading, §26.4)
    PhotoObject, PhotoDescription
    HintItem, DriveHintDoc, UNGROUNDED
    EnrichContext, ModelUsageMonth, InitialBuild
    JOB_USAGE_CLASS (EnrichJob -> UsageClass), USD_CAPS (UsageClass -> monthly dollars)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Mapping, Optional, Tuple

from .drive_index_params import (
    PASSAGE_USD_PER_MONTH, PROFILE_USD_PER_MONTH, READER_USD_PER_MONTH, TRANSCRIBE_USD_PER_MONTH,
    TRIAGE_USD_PER_MONTH,
)


class JobState(str, Enum):
    """The state of one enrichment job of a record (§25.4.5): the profile job and the passage-hint job each have one.
    An agent-type model backoff is NOT a job state (§25.4.6 reading, review MINOR 2): the job stays `ready` and the
    backoff gates the enrichment queue read; `deferred` is the refresh cooldown only."""
    CURRENT = "current"       # nothing to do
    READY = "ready"           # work is due
    DEFERRED = "deferred"     # the refresh cooldown is not over: `*_defer_until`
    PARKED = "parked"         # the month's cap and the initial build are spent, for the current version
    HELD = "held"             # a hold recorded for the current version and fingerprint (passages: generation)
    NONE = "none"             # the policy gives the kind no such job, or there is nothing readable


class HoldReason(str, Enum):
    """`profile_hold` / `passages_hold` (§25.2.1). Passages are only ever held `failed` (a refused chunk gets no hints)."""
    REFUSED = "refused"
    FAILED = "failed"


class ProfileSource(str, Enum):
    """`profile_source` (§25.2.1): written by the model, or a template for notes, script pages and machine text."""
    MODEL = "model"
    TEMPLATE = "template"


class HintType(str, Enum):
    """`HintItem.type` (§25.2.2)."""
    KEYWORD = "keyword"
    QUESTION = "question"


class UsageClass(str, Enum):
    """The model classes a user's monthly dollars are kept per (§25.4.5); `add_model_usage(cls=...)` takes the value."""
    READER = "reader"
    TRIAGE = "triage"
    PROFILE = "profile"
    PASSAGE = "passage"
    TRANSCRIBE = "transcribe"   # batch transcription of recordings (§27.10): a hard cap, no initial-build allowance (G8)


class Grade(str, Enum):
    """The evidence grade of one model-written value against the file's own text (§26.4, Δ59)."""
    IN_TEXT = "in_text"            # the text holds the value as written
    IN_TEXT_AS = "in_text_as"      # the text holds it, written differently (`GradedValue.seen_as`)
    NOT_IN_TEXT = "not_in_text"    # the text does not hold it: kept, keyed apart, never exact


class GradedField(str, Enum):
    """What kind of value a grade is about; the accuracy weights are per field (§26.4). Doc type, life areas, English
    topics and the word lists are interpretation and are never graded."""
    DATE = "date"                  # a profile date (identified by its ISO form), or a brief's date
    VALUE = "value"                # a key value (identified by its printed form), or a brief's amount
    ENTITY = "entity"              # an entity (by its printed form, else its name), or a brief's party
    NUMBER = "number"              # a number term of the profile's title or what_it_is
    TOPIC_NATIVE = "topic_native"  # a native-language topic


class EnrichJob(str, Enum):
    """The job names `count_by_job_state` keys its counts by (§25.13.1)."""
    PROFILE = "profile"
    PASSAGES = "passages"


@dataclass(frozen=True)
class ProfileDate:
    """A date printed in the document; `label` from `drive_labels.DATE_LABELS`. Graded against the text (§26.4): a
    `not_in_text` date stays but never moves a file in time. `printed`: the date as the document prints it (§26.4); ""
    in a profile stored before it."""
    label: str
    iso: date
    printed: str = ""


@dataclass(frozen=True)
class ProfileEntity:
    """A named thing; `type` from `drive_labels.ENTITY_TYPES`. Graded (§26.4): `printed` in the text or the name is
    exact; a `not_in_text` entity is keyed `ef:` only. `found`: the spellings among `name` and `forms` that verification also found there. `printed` and `found` are keyed
    `e:` (exact); the other spellings `ef:`, the index's own, never exact (§25.2.4, xc F1)."""
    name: str
    type: str
    printed: str
    forms: Tuple[str, ...] = ()
    found: Tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyValue:
    """A printed value; `label` from `drive_labels.VALUE_LABELS`. Graded (§26.4): `canonical` among the number terms
    or `printed` occurring verbatim is `in_text`; a `not_in_text` value is keyed `va:`, never `v:`."""
    label: str
    printed: str
    canonical: str


@dataclass(frozen=True)
class DriveProfile:
    """What a text file is (§25.2.1, §25.4.1). `doc_type` from `drive_labels.DOC_TYPES`, `life_domains` from
    `drive_labels.LIFE_DOMAINS`. The word lists are the index-time translations (P8): each in en, es, uk and ru,
    singular and plural; `validity_words` only when a validity or due date is printed. Descriptive fields and word
    lists are unverifiable: automatic."""
    title: str
    what_it_is: str
    language: str
    doc_type: str
    life_domains: Tuple[str, ...] = ()
    dates: Tuple[ProfileDate, ...] = ()
    entities: Tuple[ProfileEntity, ...] = ()
    key_values: Tuple[KeyValue, ...] = ()
    topics: Tuple[str, ...] = ()
    topics_native: Tuple[str, ...] = ()
    topic_words: Tuple[str, ...] = ()
    label_words: Tuple[str, ...] = ()
    domain_words: Tuple[str, ...] = ()
    validity_words: Tuple[str, ...] = ()
    graded: Tuple["GradedValue", ...] = ()     # one entry per graded value (§26.4); () in a profile stored before it


@dataclass(frozen=True)
class GradedValue:
    """The grade of one value of a profile (or of a brief): `field` a `GradedField` value, `value` its identity (a
    date's ISO form, a key value's or entity's printed form, a number term, a topic), `grade` a `Grade` value,
    `seen_as` the text's own form of it for `in_text_as` ("" otherwise). Plain strings: stored as they are."""
    field: str
    value: str
    grade: str
    seen_as: str = ""


def is_unconfirmed(profile: DriveProfile, field: GradedField, value: str) -> bool:
    """True when the profile graded this value and the file's text does not hold it (every entry of the value says
    `not_in_text`). A profile stored before grading has no grades: nothing is unconfirmed (it was verified then)."""
    grades = [g.grade for g in profile.graded if g.field == field.value and g.value == value]
    return bool(grades) and all(g == Grade.NOT_IN_TEXT.value for g in grades)


@dataclass(frozen=True)
class PhotoObject:
    """An object the triage saw in a photograph, with up to six spellings (§25.4.3). Automatic."""
    name: str
    forms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PhotoDescription:
    """Enriched triage of a photograph (§25.4.3, `TRIAGE_VERSION` 2): replaces the v1 caption. Automatic."""
    description: str
    objects: Tuple[PhotoObject, ...] = ()
    scene: str = ""
    visible_text: str = ""
    scene_labels: Tuple[str, ...] = ()   # D40: SCENE_LABELS values the model gave, deduped, at most PHOTO_SCENE_LABELS_MAX


@dataclass(frozen=True)
class HintItem:
    """One passage keyword or question (§25.2.2): `type` is a `HintType` value; `evidence` is the verbatim quote and
    `[evidence_start, evidence_end)` its span in the chunk text, set by grounding (§25.4.2). A model reply carries
    ungrounded items: both span ends are UNGROUNDED (-1) until `drive_hints.ground` sets them."""
    text: str
    type: str
    evidence: str
    evidence_start: int = -1
    evidence_end: int = -1


@dataclass(frozen=True)
class DriveHintDoc:
    """One hint document per chunk with passage hints (§25.2.2), doc id `{user_id}:{item_id}:{generation}:{index}`.
    Fenced like chunks (§7.4): readable only while the record's `indexed_generation` equals `generation`. The
    `hint_vector` travels with writes, never inside this object (as chunk vectors do)."""
    user_id: str
    item_id: str
    generation: int
    index: int
    kind: str
    chunk_text_hash: str
    items: Tuple[HintItem, ...]
    hint_keys: Tuple[str, ...]
    passages_version: int
    # MULTI_DRIVE_RFC §8 (E8): the connection; doc id `chunk_doc_id(scope, …)`, `user_id` checked on read (I3)
    connection_id: str = ""


@dataclass(frozen=True)
class EnrichContext:
    """What `profile_plan` and `passages_plan` decide with besides the record (§25.4.5); reaches the planner through
    `PlanContext`. `month_usd` and `caps_usd` are keyed by `UsageClass` values."""
    now: datetime
    month_usd: Mapping[str, float]
    caps_usd: Mapping[str, float]
    initial_balance_usd: float
    initial_open: bool


@dataclass(frozen=True)
class ModelUsageMonth:
    """`{prefix}drive_model_usage/{user_id}:{YYYY-MM}` (§25.4.5), priced at the model that ran. `daily` maps
    `YYYY-MM-DD` to `(total_usd, fallback_usd)` (the fallback alert). `<cls>_usd` is every dollar the class spent this
    month (the Cabinet, the alerts); `<cls>_initial_usd` the share of it the initial build paid (D27), which the
    month's cap never counts: the build is spent first, before the caps (review rv_sv2_p5 M1)."""
    reader_usd: float = 0.0
    triage_usd: float = 0.0
    profile_usd: float = 0.0
    passage_usd: float = 0.0
    fallback_usd: float = 0.0
    calls: int = 0
    daily: Mapping[str, Tuple[float, float]] = field(default_factory=dict)
    reader_initial_usd: float = 0.0
    triage_initial_usd: float = 0.0
    profile_initial_usd: float = 0.0
    passage_initial_usd: float = 0.0
    transcribe_usd: float = 0.0
    transcribe_initial_usd: float = 0.0     # always 0: the build never pays transcription (G8); `cap_usd` reads it
    transcribe_reserved_usd: float = 0.0    # open batch reservations made in this month (Δ106); not spent yet

    def cap_usd(self, cls: str) -> float:
        """What the month's cap of `cls` has paid: the class's dollars less the initial build's share."""
        name = UsageClass(cls).value
        return max(0.0, float(getattr(self, f"{name}_usd")) - float(getattr(self, f"{name}_initial_usd")))


@dataclass(frozen=True)
class InitialBuild:
    """`{prefix}drive_model_usage/{user_id}:initial` (§25.4.5, D27): spent first, closes once and never reopens (only
    an owner script resets it)."""
    allowance_usd: float
    spent_usd: float = 0.0
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


UNGROUNDED = -1                               # HintItem span ends before grounding

# Which monthly class pays for a job (§25.4.5); the reader and triage classes have no enrichment job.
JOB_USAGE_CLASS: Mapping[str, str] = {EnrichJob.PROFILE.value: UsageClass.PROFILE.value,
                                      EnrichJob.PASSAGES.value: UsageClass.PASSAGE.value}

# The monthly dollar cap per class (§25.4.5): `EnrichContext.caps_usd`, the planner and the Cabinet read this one map.
USD_CAPS: Mapping[str, float] = {UsageClass.READER.value: READER_USD_PER_MONTH,
                                 UsageClass.TRIAGE.value: TRIAGE_USD_PER_MONTH,
                                 UsageClass.PROFILE.value: PROFILE_USD_PER_MONTH,
                                 UsageClass.PASSAGE.value: PASSAGE_USD_PER_MONTH,
                                 UsageClass.TRANSCRIBE.value: TRANSCRIBE_USD_PER_MONTH}
