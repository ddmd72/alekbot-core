"""
Drive key sources — what the Search v2 keyer reads from a record and what it reports per about key
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC §25.2.4 "Where keys live", §25.5.6 match source). Pure data.

`about_keys_of(rec)` and `about_sources(rec)` (`drive_keys`) take any object with the `KeyedRecord` attributes: the
§25.2.1 record fields. `RecordKeyInputs` is a plain carrier of them (tests, and callers holding the fields apart from
a record).

Public interface:
    AboutField          which record field gave an about key
    AboutSource         (field, detail, printed, source_class) of one about key; recomputed at render time
    KeyedRecord         the record attributes the keyer reads (Protocol)
    RecordKeyInputs     a frozen carrier of those attributes
    effective_doc_type(rec), effective_life_areas(rec)   the owner's classifiers over the profile's, read at key and
                        render time (DRIVE_FILE_NOTES_RFC §5.4, D7); the stored profile is never changed
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, Protocol, Tuple

from .drive_enrichment import DriveProfile, PhotoObject
from .drive_index import TriageVerdict
from .drive_match import SourceClass


class AboutField(str, Enum):
    """The record field an about key came from (§25.5.6: "place "Rome" (named in the file)", "type label ...")."""
    ENTITY = "entity"                     # a spelling the file prints of a profile entity; `detail` is its type
    ENTITY_FORM = "entity_form"           # a spelling the profiler wrote that the file does not print (`ef:`, xc F1)
    PLACE = "place"                       # the photo location's `place_forms`
    VALUE = "value"                       # verified key value; `detail` is its label
    DATE = "date"                         # verified date; `detail` is its label
    EFFECTIVE_DATE = "effective_date"     # the record's effective date (year, month)
    KIND = "kind"                         # the k: table
    LABEL = "label"                       # l: keys (grouping only)
    LABEL_WORD = "label_word"             # automatic below
    TOPIC_WORD = "topic_word"
    DOMAIN_WORD = "domain_word"
    VALIDITY_WORD = "validity_word"
    PHOTO_WORD = "photo_word"             # description, scene, objects, visible text; `detail` names which
    PHOTO_OBJECT = "photo_object"         # an object's name or spelling, as a phrase
    VISIBLE_TEXT_NUMBER = "visible_text_number"
    CAPTURE = "capture"                   # the capture's names (§26.3); `detail` is the capture value
    SCENE_LABEL = "scene_label"           # a scene label's names (D40); `detail` is the label
    # the owner's note on the item (DRIVE_FILE_NOTES_RFC §5.4): `e:` / `v:` / `y:` / `m:` keys (About) and `ex:` / `exl:`
    # skeletons (By translation); `detail` is "type" / "area" (a classifier name), "number", "date" or "word"
    OWNER_NOTE = "owner_note"


@dataclass(frozen=True)
class AboutSource:
    """One source of one about key: the field, a detail (entity type, value or date label, photo field), the spelling
    the key was made from, the source class it proves (§25.5.4; None for `l:` keys, which no query produces), and,
    for an entity spelling the file does not print, what the file prints instead (`in_file`, xc F1)."""
    field: AboutField
    detail: str
    printed: str
    source_class: Optional[SourceClass]
    in_file: str = ""


class KeyedRecord(Protocol):
    """The record attributes the keyer reads (§25.2.1)."""

    @property
    def kind(self) -> str: ...

    @property
    def triage_verdict(self) -> Optional[TriageVerdict]: ...

    @property
    def effective_date(self) -> Optional[date]: ...

    @property
    def profile(self) -> Optional[DriveProfile]: ...

    @property
    def valid_until(self) -> Optional[date]: ...

    @property
    def place_forms(self) -> Tuple[str, ...]: ...

    @property
    def photo_description(self) -> str: ...

    @property
    def photo_objects(self) -> Tuple[PhotoObject, ...]: ...

    @property
    def photo_scene(self) -> str: ...

    @property
    def photo_scene_labels(self) -> Tuple[str, ...]: ...

    @property
    def visible_text(self) -> str: ...

    @property
    def capture(self) -> str: ...

    @property
    def capture_source(self) -> str: ...

    @property
    def owner_note_text(self) -> str: ...

    @property
    def owner_doc_type(self) -> str: ...

    @property
    def owner_life_areas(self) -> Tuple[str, ...]: ...


@dataclass(frozen=True)
class RecordKeyInputs:
    """A `KeyedRecord` carrier: the §25.2.1 fields the keyer reads, nothing else."""
    kind: str
    triage_verdict: Optional[TriageVerdict] = None
    effective_date: Optional[date] = None
    profile: Optional[DriveProfile] = None
    valid_until: Optional[date] = None
    place_forms: Tuple[str, ...] = ()
    photo_description: str = ""
    photo_objects: Tuple[PhotoObject, ...] = ()
    photo_scene: str = ""
    photo_scene_labels: Tuple[str, ...] = ()
    visible_text: str = ""
    capture: str = ""                  # §26.3: a drive_capture.Capture value, or ""
    capture_source: str = ""           # a drive_capture.CaptureSource value, or ""
    # DRIVE_FILE_NOTES_RFC §5.4: the record's copy of the owner's note; empty by default, so the golden record emits
    # no note keys and `golden_digest` stays pinned
    owner_note_text: str = ""
    owner_doc_type: str = ""
    owner_life_areas: Tuple[str, ...] = ()


def effective_doc_type(rec: KeyedRecord) -> str:
    """The doc type search groups and reads by (D7): the owner's when he gave one, else the profile's ("" without
    either)."""
    if rec.owner_doc_type:
        return rec.owner_doc_type
    return rec.profile.doc_type if rec.profile is not None else ""


def effective_life_areas(rec: KeyedRecord) -> Tuple[str, ...]:
    """The life areas search groups and reads by (D7): the owner's when he gave any, else the profile's."""
    if rec.owner_life_areas:
        return tuple(rec.owner_life_areas)
    return tuple(rec.profile.life_domains) if rec.profile is not None else ()
