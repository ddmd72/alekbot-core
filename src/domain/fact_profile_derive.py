"""From the profiler's answer to a fact's stored profile (fact profile RFC §3.3, §4.4; taxonomy §5.2).

Code decides the domains from the model's answers (`derive_domains`, the taxonomy's precedence verbatim) and checks every
value the model proposes against the fact's own text and metadata; a value that fails a check is dropped and counted
per reason in `profile_drops`:

- label words: lowercased, at most 3 words, no digits, not a lone record word, not a relation word, not only word forms
  the text or metadata already holds, no duplicate, at most LABEL_CAP;
- time points: the source is printed and the date is read from it (`drive_grade.TextEvidence`), `from <= to`, years
  YEAR_MIN..YEAR_MAX, no duplicate, at most TIME_CAP;
- places: the source is printed, the city found in the named country by the atlas (`place_names.PlaceNames`); one row
  gives keys, anything else is kept as unresolved;
- people: the name (or, without a name, the source) is printed; keys from the drive key forms.

A directive is decided by the consolidator, not asked of the model: it gets `agent_directives`, no extras and nothing
else (`directive_profile`). An `intimate` answer with no surviving label word is a profiling error that needs a redo:
the outcome carries `redo_reason` and nothing of it may be stored but the failure.
"""
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .drive_content_terms import _MONTH_OF, content_terms
from .drive_enrichment import Grade
from .drive_grade import TextEvidence
from .drive_key_forms import primary as form_primary
from .drive_key_forms import skeleton_key, token_spans
from .drive_keys import _month_year_spans, word_form_keys
from .fact_domains import Domain
from .fact_profile import FactProfile, ProfilePerson, ProfileTimePoint, UnresolvedPlace
from .fact_profile_answer import AnswerPerson, AnswerPlace, AnswerTimePoint, ProfileAnswer
from .fact_profile_fingerprint import metadata_lines, metadata_value
from .place_names import PlaceNames

LABEL_CAP = 12
LABEL_MAX_WORDS = 3
TIME_CAP = 8
USE_AREAS_CAP = 2
TOPICAL_CAP = 2
YEAR_MIN, YEAR_MAX = 1900, 2100
INTIMATE_WITHOUT_LABELS = "intimate_without_labels"
# Words that name a record, not a life situation: a lone one bridges nothing.
RECORD_WORDS = frozenset({"fact", "record", "entry", "note", "data", "details", "information", "status", "metadata",
                          "user"})
# Relations belong to the people field; compared by their primary form, so plurals and possessives match too.
RELATION_WORDS = frozenset({
    "wife", "husband", "spouse", "son", "daughter", "mother", "father", "mom", "mum", "dad", "brother", "sister",
    "sibling", "parent", "child", "children", "kid", "partner", "friend", "girlfriend", "boyfriend", "fiance",
    "fiancee", "grandmother", "grandfather", "grandparent", "grandma", "grandpa", "grandson", "granddaughter",
    "grandchild", "grandchildren", "aunt", "uncle", "cousin", "niece", "nephew", "stepmother", "stepfather",
    "stepson", "stepdaughter", "stepbrother", "stepsister", "relative"})
_FOUR_DIGITS = re.compile(r"(?<!\d)\d{4}(?!\d)")


@dataclass(frozen=True)
class DeriveOutcome:
    """The derived profile; `redo_reason` set when the answer is a profiling error that needs another call."""
    profile: FactProfile
    redo_reason: Optional[str] = None


def _bump(drops: Dict[str, int], reason: str, n: int = 1) -> None:
    drops[reason] = drops.get(reason, 0) + n


def _dedupe(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(items))


# ── Domains (taxonomy §5.2) ─────────────────────────────────────────────────

def derive_domains(answer: ProfileAnswer) -> Tuple[str, Tuple[str, ...], bool]:
    """(primary, extras, profiling_error) of a non-directive fact: tax/fine, identity, pet, state/law, person, then the
    subject; hard extras uncapped, topical extras at most TOPICAL_CAP. `none` with no flag set reads as lifestyle and
    is a profiling error."""
    hard: List[str] = []
    primary: Optional[str] = None
    if answer.tax_or_fine == "tax":
        primary, hard = Domain.MONEY, [Domain.STATE_LAW]
    elif answer.tax_or_fine == "fine":
        primary, hard = Domain.STATE_LAW, [Domain.MONEY]
    hits: List[str] = []
    if answer.user_identity:
        hits.append(Domain.BIOGRAPHY)
    if answer.pet:
        hits.append(Domain.PETS)
    if answer.state_or_law:
        hits.append(Domain.STATE_LAW)
    if answer.about_person == "relative":
        hits.append(Domain.FAMILY)
    elif answer.about_person == "other":
        hits.append(Domain.PEOPLE)
    flag_decided = primary is not None or bool(hits)
    subject, error = answer.subject, False
    if primary is None and not hits and subject == "none":
        subject, error = Domain.LIFESTYLE, True
    if primary is None:
        primary = hits[0] if hits else subject
    if answer.money_flow:
        hard.append(Domain.MONEY)
    hard = _dedupe([h for h in hard if h != primary])
    topical = ([subject] if flag_decided and subject != "none" else []) + hits + answer.use_areas[:USE_AREAS_CAP]
    excluded = {primary, Domain.AGENT_DIRECTIVES, *hard}
    topical = _dedupe([t for t in topical if t not in excluded])[:TOPICAL_CAP]
    return primary, tuple(hard + topical), error


def directive_profile() -> FactProfile:
    """A standing directive: agent_directives alone, no model answer, nothing else (no call is made)."""
    return FactProfile(primary_domain=Domain.AGENT_DIRECTIVES, domain_keys=(Domain.AGENT_DIRECTIVES,),
                       user_identity=False, kinds=(), sensitivity="ordinary")


# ── Label words ─────────────────────────────────────────────────────────────

def check_labels(words: Sequence[str], known_forms: Set[str], drops: Dict[str, int]) -> Tuple[str, ...]:
    """The label words that survive, in the model's order; `known_forms` are the `w:` keys of the text and metadata."""
    kept: List[str] = []
    kept_primaries: List[Tuple[str, ...]] = []
    for word in words:
        label = " ".join(word.casefold().split())
        tokens = label.split()
        if not label:
            _bump(drops, "label_empty")
            continue
        if len(tokens) > LABEL_MAX_WORDS:
            _bump(drops, "label_too_long")
            continue
        if re.search(r"\d", label):
            _bump(drops, "label_has_number")
            continue
        if len(tokens) == 1 and tokens[0] in RECORD_WORDS:
            _bump(drops, "label_record_word")
            continue
        if not word_form_keys(label):
            _bump(drops, "label_no_keys")
            continue
        spans = [t for t, _, _ in token_spans(label)]
        if any(form_primary(t) in RELATION_WORDS or t in RELATION_WORDS for t in spans):
            _bump(drops, "label_relation_word")
            continue
        forms = [f for f in (set(word_form_keys(t)) for t in spans) if f]
        if forms and all(f & known_forms for f in forms):
            _bump(drops, "label_repeats_text")
            continue
        primaries = tuple(form_primary(t) for t in spans)
        if primaries in kept_primaries:
            _bump(drops, "label_duplicate")
            continue
        if len(kept) >= LABEL_CAP:
            _bump(drops, "label_over_cap")
            continue
        kept.append(label)
        kept_primaries.append(primaries)
    return tuple(kept)


# ── Time points ─────────────────────────────────────────────────────────────

def _has_word(haystack: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", haystack) is not None


def _month_forms(month: int) -> Set[str]:
    return {form for form, m in _MONTH_OF.items() if m == month}


def _year_printed(source: str, raw_all: str, year: int) -> bool:
    """The year in the source, or (when the source prints no year at all) anywhere in the text."""
    return _has_word(source, str(year)) or (not _FOUR_DIGITS.search(source) and _has_word(raw_all, str(year)))


def check_date(evidence: TextEvidence, raw_all: str, metadata_values: Sequence[str], value: str, precision: str,
               source: str) -> Tuple[bool, str]:
    """(accepted, how it was read or the drop reason) of one ISO value read from `source`."""
    src = source.strip()
    src_printed = bool(src) and evidence.occurs(src)
    s = src.casefold()
    try:
        if precision == "day":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(value)
            day = date.fromisoformat(value)
            if not YEAR_MIN <= day.year <= YEAR_MAX:
                return False, "time_year_out_of_range"
            grade, _ = evidence.date_grade(day, src)
            if grade in (Grade.IN_TEXT, Grade.IN_TEXT_AS):
                return True, "date_grade"
            if not src_printed:
                return False, "time_source_not_printed"
            day_ok = (_has_word(s, str(day.day)) or _has_word(s, f"{day.day:02d}")
                      or re.search(rf"(?<!\d){day.day}(?:st|nd|rd|th)?\s*[-–]", s) is not None)
            month_ok = any(_has_word(s, f) for f in _month_forms(day.month))
            if day_ok and month_ok and _year_printed(s, raw_all, day.year):
                return True, "parts"
            return False, "time_date_not_in_source"
        if precision == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", value):
                raise ValueError(value)
            year, month = int(value[:4]), int(value[5:])
            if not YEAR_MIN <= year <= YEAR_MAX:
                return False, "time_year_out_of_range"
            if src == value and value in metadata_values:
                return True, "metadata_yyyy_mm"
            if not src_printed:
                return False, "time_source_not_printed"
            if any(ym == value for _, _, ym in _month_year_spans(src)):
                return True, "month_year_span"
            if any(t.startswith(value) for t in content_terms(src)):
                return True, "iso_in_month"
            if any(_has_word(s, f) for f in _month_forms(month)) and _year_printed(s, raw_all, year):
                return True, "parts"
            if re.search(rf"(?<!\d){year}-{month:02d}(?!\d)", src):
                return True, "yyyy_mm_in_source"
            return False, "time_date_not_in_source"
        if precision == "year":
            if not re.fullmatch(r"\d{4}", value):
                raise ValueError(value)
            if not YEAR_MIN <= int(value) <= YEAR_MAX:
                return False, "time_year_out_of_range"
            if not src_printed:
                return False, "time_source_not_printed"
            if re.search(rf"(?<!\d){value}(?!\d)", src):
                return True, "year"
            return False, "time_date_not_in_source"
    except ValueError:
        return False, "time_bad_format"
    return False, "time_bad_format"


def check_time_points(points: Sequence[AnswerTimePoint], evidence: TextEvidence, raw_all: str,
                      metadata_values: Sequence[str], drops: Dict[str, int]) -> Tuple[ProfileTimePoint, ...]:
    kept: List[ProfileTimePoint] = []
    seen: Set[Tuple[str, str]] = set()
    for point in points:
        if len(kept) >= TIME_CAP:
            _bump(drops, "time_over_cap")
            continue
        ok, why = check_date(evidence, raw_all, metadata_values, point.from_, point.precision, point.source)
        if not ok:
            _bump(drops, why)
            continue
        end = ""
        if point.to:
            ok_to, why_to = check_date(evidence, raw_all, metadata_values, point.to, point.precision, point.source)
            if ok_to and point.to >= point.from_:
                end = point.to
            else:
                _bump(drops, "time_to_" + (why_to if not ok_to else "before_from"))
        if (point.from_, end) in seen:
            _bump(drops, "time_duplicate")
            continue
        seen.add((point.from_, end))
        kept.append(ProfileTimePoint(point.from_, end, point.precision, point.source))
    return tuple(kept)


# ── Places ──────────────────────────────────────────────────────────────────

def check_places(places: Sequence[AnswerPlace], evidence: TextEvidence, atlas: PlaceNames,
                 drops: Dict[str, int]) -> Tuple[Tuple[str, ...], Tuple[UnresolvedPlace, ...]]:
    """(place keys, unresolved places) of the printed places."""
    keys: List[str] = []
    unresolved: List[UnresolvedPlace] = []
    for place in places:
        src = place.source.strip()
        if not src or not (evidence.occurs(src) or evidence.skeleton_seen(src)):
            _bump(drops, "place_not_printed")
            continue
        found, reason = atlas.resolve(place.city, place.country)
        if reason is not None:
            unresolved.append(UnresolvedPlace(place.city, place.country))
            _bump(drops, f"place_unresolved:{reason}")
        else:
            keys.extend(found)
    return tuple(_dedupe(keys)), tuple(unresolved)


# ── People ──────────────────────────────────────────────────────────────────

def name_keys(name: str) -> Tuple[str, ...]:
    """The `w:` forms of a name and an `x:` transliteration skeleton per word."""
    keys = list(word_form_keys(name))
    for token, _, _ in token_spans(name):
        skeleton = skeleton_key(token)
        if skeleton:
            keys.append("x:" + skeleton)
    return tuple(_dedupe(keys))


def check_people(people: Sequence[AnswerPerson], evidence: TextEvidence,
                 drops: Dict[str, int]) -> Tuple[ProfilePerson, ...]:
    kept: List[ProfilePerson] = []
    for person in people:
        name, role, src = person.name.strip(), person.role.strip().casefold(), person.source.strip()
        if not name and not role:
            _bump(drops, "person_empty")
            continue
        if name:
            printed = evidence.occurs(name) or bool(evidence.skeleton_seen(name))
        else:
            printed = bool(src) and evidence.occurs(src)
        if not printed:
            _bump(drops, "person_not_printed")
            continue
        kept.append(ProfilePerson(name, role, name_keys(name) if name else (),
                                  word_form_keys(role) if role else ()))
    return tuple(kept)


# ── The whole profile ───────────────────────────────────────────────────────

def _metadata_values(metadata: Optional[Mapping[str, Any]]) -> List[str]:
    items = metadata or {}
    return [metadata_value(items[key]) for key in sorted(items)]


def derive_profile(answer: ProfileAnswer, text: str, metadata: Optional[Mapping[str, Any]],
                   atlas: PlaceNames) -> DeriveOutcome:
    """The profile of a non-directive fact from its validated answer (a directive takes `directive_profile`)."""
    drops: Dict[str, int] = {}
    texts = [text, *metadata_lines(metadata)]
    evidence = TextEvidence(texts)
    raw_all = "\n".join(texts).casefold()
    values = _metadata_values(metadata)

    if len(answer.use_areas) > USE_AREAS_CAP:
        _bump(drops, "use_areas_over_cap", len(answer.use_areas) - USE_AREAS_CAP)
    primary, extras, error = derive_domains(answer)
    if error:
        _bump(drops, "subject_none_without_flag")

    known_forms = set(word_form_keys(text))
    for value in values:
        known_forms |= set(word_form_keys(value))
    labels = check_labels(answer.label_words, known_forms, drops)
    redo = None
    if answer.sensitivity == "intimate" and not labels:
        _bump(drops, INTIMATE_WITHOUT_LABELS)
        redo = INTIMATE_WITHOUT_LABELS

    points = check_time_points(answer.time_points, evidence, raw_all, values, drops)
    place_keys, unresolved = check_places(answer.places, evidence, atlas, drops)
    people = check_people(answer.people, evidence, drops)
    profile = FactProfile(
        primary_domain=primary, domain_keys=(primary, *extras), user_identity=answer.user_identity,
        kinds=tuple(answer.kinds), sensitivity=answer.sensitivity, label_words=labels, time_role=answer.time_role,
        time_points=points, place_keys=place_keys, places_unresolved=unresolved, people=people,
        profile_answer=answer.stored(), profile_drops=drops,
    )
    return DeriveOutcome(profile, redo)
