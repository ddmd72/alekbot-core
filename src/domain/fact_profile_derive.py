"""From the profiler's answer to a fact's stored profile (fact profile RFC §3.3, §4.4; taxonomy §5.2).

Code decides the domains from the model's answers (`derive_domains`, the taxonomy's precedence verbatim) and checks every
value the model proposes against the fact's own text and metadata; a value that fails a check is dropped and counted
per reason in `profile_drops`:

- label words: lowercased, at most 3 words, no digits, not a lone record word, not a relation word, not only word forms
  the text or metadata already holds, no duplicate, at most LABEL_CAP;
- time points (a quarter or season source is its range of months, `fact_profile_periods`): the source is printed and the date is read from it (`drive_grade.TextEvidence`), `from <= to`, years
  YEAR_MIN..YEAR_MAX, no duplicate, at most TIME_CAP;
- places: the source is printed, the city found in the named country by the atlas (`place_names.PlaceNames`); one row
  gives keys, anything else is kept as unresolved;
- people: the name (or, without a name, the source) is printed; keys from the drive key forms. A name the fact does
  not print is kept only when it is a confirmed list entry's name and the source (the printed relation) is printed; the
  person is then marked `from_list`;
- a date the fact prints without a year is written `--MM-DD` / `--MM` by the model and takes the recording year
  (`recording_year`); a month or year read from a printed full date is accepted (an approximate time is filed there);
- review items: kept only when the mention is printed and the answer is `needs_review`;
- the owner's answers to profile questions (§4.7, `apply_answers`): a place answer before the atlas, a person or date
  answer of this lineage on its review item; an answered item is dropped, and the flag clears when none is left.

A directive is decided by the consolidator, not asked of the model: it gets `agent_directives`, no extras and nothing
else (`directive_profile`). An `intimate` answer with no surviving label word is a profiling error that needs a redo:
the outcome carries `redo_reason` and nothing of it may be stored but the failure.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .confirmed_people import ConfirmedPerson
from .drive_content_terms import _MONTH_OF, content_terms
from .drive_enrichment import Grade
from .drive_grade import TextEvidence
from .drive_key_forms import primary as form_primary
from .drive_key_forms import skeleton_key, token_spans
from .drive_keys import _month_year_spans, word_form_keys
from .fact_domains import Domain
from .fact_profile import FactProfile, ProfilePerson, ProfileReviewItem, ProfileTimePoint, UnresolvedPlace
from .fact_profile_answer import AnswerPerson, AnswerPlace, AnswerTimePoint, ProfileAnswer
from .fact_profile_periods import period_ranges
from .fact_profile_fingerprint import metadata_lines, metadata_value
from .place_names import PlaceNames, country_key, fold, local_place_key
from .profile_answers import EMPTY_ANSWERS, DateAnswer, ProfileAnswers, date_point_key, person_point_key, place_point_key

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
# A range whose end is printed short ("2026–27", "2026-27"): the end is the year of the start's century.
_SHORT_RANGE = re.compile(r"(?<!\d)(\d{4})\s*[-–—]\s*(\d{2})(?!\d)")
# The prefix the fact review writes on a retracted fact (`fact_review_answers.ended_text`): the month in it is when the
# owner said the fact had ended, not a time the fact is about.
_RETRACTION_PREFIX = re.compile(r"^\s*No longer true \([^)\n]*\bsaid so in [^)\n]*\):\s*")


_YEARLESS_DAY = re.compile(r"--\d{2}-\d{2}")
_YEARLESS_MONTH = re.compile(r"--\d{2}")


def recording_year(reported_date: datetime, created_at: datetime) -> int:
    """The UTC year of the earlier of the two (a naive value reads as UTC). The earlier, because a stored document that
    lacks `reported_date` reads it as the time of the read, and that is the year of the run, not of the fact."""
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return min(utc(reported_date), utc(created_at)).year


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
    return _has_word(source, str(year)) or (not _FOUR_DIGITS.search(source)
                                            and _has_word(raw_all.replace("_", " "), str(year)))


def _short_range_ends(source: str) -> Set[str]:
    """The full years the short range ends in `source` stand for: "2026–27" gives 2027, in the start's century (an end
    below the start belongs to the next century: "2099–01" gives 2101)."""
    ends: Set[str] = set()
    for start, end in _SHORT_RANGE.findall(source):
        year = int(start[:2] + end)
        ends.add(str(year if year >= int(start) else year + 100))
    return ends


def date_value_problem(value: str, precision: str) -> Optional[str]:
    """None when `value` is an ISO value of `precision` as the profiler writes it (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`) with
    its year in YEAR_MIN..YEAR_MAX; else the drop reason (`time_bad_format`, `time_year_out_of_range`). The one reader
    of a date's form: `check_date` reads the model's values with it, `owner_time_point` the owner's date answers."""
    try:
        if precision == "day":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return "time_bad_format"
            year = date.fromisoformat(value).year
        elif precision == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", value):
                return "time_bad_format"
            year = int(value[:4])
        elif precision == "year":
            if not re.fullmatch(r"\d{4}", value):
                return "time_bad_format"
            year = int(value)
        else:
            return "time_bad_format"
    except ValueError:
        return "time_bad_format"
    return None if YEAR_MIN <= year <= YEAR_MAX else "time_year_out_of_range"


def check_date(evidence: TextEvidence, raw_all: str, metadata_values: Sequence[str], value: str, precision: str,
               source: str, year_supplied: bool = False) -> Tuple[bool, str]:
    """(accepted, how it was read or the drop reason) of one ISO value read from `source`. With `year_supplied` the
    year of `value` is the recording year, not a printed one: the source then proves the day and month only."""
    problem = date_value_problem(value, precision)
    if problem is not None:
        return False, problem
    src = source.strip()
    src_printed = bool(src) and evidence.occurs(src)
    # A date copied from metadata keeps its underscores ("aug_2025", "Aug_14_2025"): read it as its text form.
    s = src.casefold().replace("_", " ")
    spaced = src.replace("_", " ")
    try:
        if precision == "day":
            day = date.fromisoformat(value)
            grade, _ = evidence.date_grade(day, spaced)
            if grade in (Grade.IN_TEXT, Grade.IN_TEXT_AS):
                return True, "date_grade"
            if not src_printed:
                return False, "time_source_not_printed"
            day_ok = (_has_word(s, str(day.day)) or _has_word(s, f"{day.day:02d}")
                      or re.search(rf"(?<!\d){day.day}(?:st|nd|rd|th)?\s*[-–]", s) is not None)
            month_ok = any(_has_word(s, f) for f in _month_forms(day.month))
            if day_ok and month_ok and (year_supplied or _year_printed(s, raw_all, day.year)):
                return True, "parts_recorded_year" if year_supplied else "parts"
            return False, "time_date_not_in_source"
        if precision == "month":
            year, month = int(value[:4]), int(value[5:])
            if src == value and value in metadata_values:
                return True, "metadata_yyyy_mm"
            if not src_printed:
                return False, "time_source_not_printed"
            if any(value in ends for ends in period_ranges(spaced)):
                return True, "quarter_or_season"
            if any(ym == value for _, _, ym in _month_year_spans(spaced)):
                return True, "month_year_span"
            if any(t.startswith(value) for t in content_terms(spaced)):
                return True, "iso_in_month"
            if any(_has_word(s, f) for f in _month_forms(month)) and (
                    year_supplied or _year_printed(s, raw_all, year)):
                return True, "parts_recorded_year" if year_supplied else "parts"
            if re.search(rf"(?<!\d){year}[-_]{month:02d}(?!\d)", src):
                return True, "yyyy_mm_in_source"
            return False, "time_date_not_in_source"
        if precision == "year":
            if not src_printed:
                return False, "time_source_not_printed"
            if re.search(rf"(?<!\d){value}(?!\d)", src):
                return True, "year"
            if value in _short_range_ends(src):
                return True, "year_short_range_end"
            return False, "time_date_not_in_source"
    except ValueError:
        return False, "time_bad_format"
    return False, "time_bad_format"


def _yearless(value: str, precision: str) -> Optional[bool]:
    """True: `value` is a yearless date of its precision; False: a full value; None: it starts like a yearless date but
    is not one of this precision."""
    if not value.startswith("--"):
        return False
    pattern = {"day": _YEARLESS_DAY, "month": _YEARLESS_MONTH}.get(precision)
    return True if pattern is not None and pattern.fullmatch(value) else None


def _check_point_end(value: str, year: Optional[int], precision: str, source: str, evidence: TextEvidence,
                     raw_all: str, metadata_values: Sequence[str]) -> Tuple[bool, str, str]:
    """(accepted, how or the drop reason, the ISO value) of one end of a point; a yearless end takes `year`."""
    yearless = _yearless(value, precision)
    if yearless is None:
        return False, "time_bad_format", value
    if not yearless:
        ok, why = check_date(evidence, raw_all, metadata_values, value, precision, source)
        return ok, why, value
    if year is None:
        return False, "time_year_unknown", value
    if _FOUR_DIGITS.search(source):
        return False, "time_yearless_but_year_printed", value
    filled = f"{year:04d}{value[1:]}"
    ok, why = check_date(evidence, raw_all, metadata_values, filled, precision, source, year_supplied=True)
    return ok, why, filled


def check_time_points(points: Sequence[AnswerTimePoint], evidence: TextEvidence, raw_all: str,
                      metadata_values: Sequence[str], drops: Dict[str, int],
                      recorded_year: Optional[int] = None) -> Tuple[ProfileTimePoint, ...]:
    """The points that survive. A date written without a year (`--MM-DD` for a day, `--MM` for a month) takes
    `recorded_year`; when both ends are yearless and the end then falls before the start, the end is in the next year."""
    kept: List[ProfileTimePoint] = []
    seen: Set[Tuple[str, str]] = set()
    for point in points:
        if len(kept) >= TIME_CAP:
            _bump(drops, "time_over_cap")
            continue
        ok, why, start = _check_point_end(point.from_, recorded_year, point.precision, point.source, evidence, raw_all,
                                          metadata_values)
        if not ok:
            _bump(drops, why)
            continue
        end = ""
        if point.to:
            to_year = recorded_year
            if (recorded_year is not None and _yearless(point.from_, point.precision)
                    and _yearless(point.to, point.precision)):
                filled_to = f"{recorded_year:04d}{point.to[1:]}"
                if filled_to < start:
                    to_year = recorded_year + 1
            ok_to, why_to, filled = _check_point_end(point.to, to_year, point.precision, point.source, evidence,
                                                    raw_all, metadata_values)
            if ok_to and filled >= start:
                end = filled
            else:
                _bump(drops, "time_to_" + (why_to if not ok_to else "before_from"))
        if (start, end) in seen:
            _bump(drops, "time_duplicate")
            continue
        seen.add((start, end))
        kept.append(ProfileTimePoint(start, end, point.precision, point.source))
    return tuple(kept)


# ── Places ──────────────────────────────────────────────────────────────────

def check_places(places: Sequence[AnswerPlace], evidence: TextEvidence, atlas: PlaceNames,
                 drops: Dict[str, int], answers: ProfileAnswers = EMPTY_ANSWERS,
                 ) -> Tuple[Tuple[str, ...], Tuple[UnresolvedPlace, ...]]:
    """(place keys, unresolved places) of the printed places; the owner's place answers are applied first (§4.7)."""
    keys, unresolved, _ = _check_places(places, evidence, atlas, drops, answers)
    return keys, unresolved


def _answered_place(place: AnswerPlace, atlas: PlaceNames, answers: ProfileAnswers,
                    drops: Dict[str, int]) -> Optional[Tuple[str, ...]]:
    """The keys the owner's answer gives this place, or None when no answer places it (none, `unknown`, or an `atlas`
    row that has left the table: counted, and the place falls back to resolution)."""
    if not answers.places:
        return None
    key = place_point_key(place.city, atlas.canonical_country(place.country) or place.country)
    entry = answers.places.get(key)
    if entry is None or entry.resolution == "unknown":
        return None
    if entry.resolution == "atlas":
        found = atlas.keys_of(entry.geonameid)
        if not found:
            _bump(drops, "place_answer_stale")
            return None
        _bump(drops, "place_answered:atlas")
        return found
    local = [local_place_key(entry.city)]
    canonical = atlas.canonical_country(entry.country) if entry.country.strip() else None
    if canonical:
        local.append(country_key(canonical))
    _bump(drops, "place_answered:local")
    return tuple(local)


def _check_places(places: Sequence[AnswerPlace], evidence: TextEvidence, atlas: PlaceNames, drops: Dict[str, int],
                  answers: ProfileAnswers) -> Tuple[Tuple[str, ...], Tuple[UnresolvedPlace, ...], Set[str]]:
    """`check_places` plus the folded cities the owner's answers placed (their `places` review items are answered)."""
    keys: List[str] = []
    unresolved: List[UnresolvedPlace] = []
    answered: Set[str] = set()
    for place in places:
        src = place.source.strip()
        if not src or not (evidence.occurs(src) or evidence.skeleton_seen(src)):
            _bump(drops, "place_not_printed")
            continue
        placed = _answered_place(place, atlas, answers, drops)
        if placed is not None:
            keys.extend(placed)
            answered.add(fold(place.city))
            continue
        found, reason = atlas.resolve(place.city, place.country)
        if reason is not None:
            unresolved.append(UnresolvedPlace(place.city, place.country))
            _bump(drops, f"place_unresolved:{reason}")
            # The model's country survives a city the atlas lacks or cannot pick; a city the atlas places only in
            # another country contradicts it, and then neither is trusted.
            if place.country.strip() and reason in ("not_in_atlas", "ambiguous"):
                keys.extend(atlas.resolve("", place.country)[0])
        else:
            keys.extend(found)
    return tuple(_dedupe(keys)), tuple(unresolved), answered


# ── People ──────────────────────────────────────────────────────────────────

def name_keys(name: str) -> Tuple[str, ...]:
    """The `w:` forms of a name and an `x:` transliteration skeleton per word."""
    keys = list(word_form_keys(name))
    for token, _, _ in token_spans(name):
        skeleton = skeleton_key(token)
        if skeleton:
            keys.append("x:" + skeleton)
    return tuple(_dedupe(keys))


def check_people(people: Sequence[AnswerPerson], evidence: TextEvidence, drops: Dict[str, int],
                 listed: Sequence[ConfirmedPerson] = ()) -> Tuple[ProfilePerson, ...]:
    """The people that survive. A name the fact does not print is kept in one case: it is the name of a confirmed list
    entry (never the user's) and the source, the printed words that pointed to the person, is printed; the person
    carries the entry's name and relation and is marked `from_list`."""
    by_name = {entry.name.casefold(): entry for entry in listed if not entry.is_user}
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
        entry = by_name.get(name.casefold()) if name and not printed else None
        if entry is not None and src and evidence.occurs(src):
            relation = entry.relation.casefold()
            kept.append(ProfilePerson(entry.name, relation, name_keys(entry.name), word_form_keys(relation),
                                      from_list=True))
            continue
        if not printed:
            _bump(drops, "person_not_printed")
            continue
        kept.append(ProfilePerson(name, role, name_keys(name) if name else (),
                                  word_form_keys(role) if role else ()))
    return tuple(kept)


# ── Review items ────────────────────────────────────────────────────────────

def check_review(answer: ProfileAnswer, evidence: TextEvidence,
                 drops: Dict[str, int]) -> Tuple[bool, Tuple[ProfileReviewItem, ...]]:
    """(needs review, the items that survive). `confident` carries no items (any given are dropped and counted);
    `needs_review` keeps the items whose mention is printed, once per (field, mention). The flag stays set when no item
    survives: the model said it is unsure, and that is worth one generic question later."""
    if answer.quality != "needs_review":
        if answer.review_items:
            _bump(drops, "review_items_without_flag", len(answer.review_items))
        return False, ()
    kept: List[ProfileReviewItem] = []
    seen: Set[Tuple[str, str]] = set()
    for item in answer.review_items:
        mention = item.mention.strip()
        if not mention or not (evidence.occurs(mention) or evidence.skeleton_seen(mention)):
            _bump(drops, "review_mention_not_printed")
            continue
        key = (item.field, mention.casefold())
        if key not in seen:
            seen.add(key)
            kept.append(ProfileReviewItem(item.field, mention))
    return True, tuple(kept)


# ── The owner's answers (§4.7) ──────────────────────────────────────────────

def owner_time_point(answer: DateAnswer) -> Optional[ProfileTimePoint]:
    """The point an owner's date answer gives, read with the derivation's own date reader (`date_value_problem`; a
    month is read as its first day too, so a month that does not exist is refused); None when it does not read or its
    end is before its start. The source is the mention the owner was asked about."""
    def reads(value: str) -> bool:
        if date_value_problem(value, answer.precision) is not None:
            return False
        return answer.precision != "month" or date_value_problem(value + "-01", "day") is None
    if not reads(answer.start) or (answer.end and (not reads(answer.end) or answer.end < answer.start)):
        return None
    return ProfileTimePoint(answer.start, answer.end, answer.precision, answer.mention)


def _source_about(source: str, mention: str) -> bool:
    """A point read from the flagged words: its folded source and the folded mention hold one another as words."""
    a, b = fold(source), fold(mention)
    if not a or not b:
        return False
    return a == b or _has_word(a, b) or _has_word(b, a)


def _person_matches(person: ProfilePerson, mention: str) -> bool:
    """The person a mention points to: the folded name equals it or holds every word of it; without a name, the folded
    role equals it."""
    if person.name:
        name = fold(person.name)
        return name == mention or set(mention.split()) <= set(name.split())
    return fold(person.role) == mention


def _apply_person(entry: ConfirmedPerson, mention: str, people: List[ProfilePerson]) -> List[ProfilePerson]:
    """The people after the owner said `mention` is `entry` (§4.5 rules): the user is no person entry; a printed name is
    kept and takes the entry's relation; a person without a name takes the entry's name, from the list; nobody matched
    adds the entry, from the list."""
    if entry.is_user:
        return [p for p in people if not _person_matches(p, mention)]
    relation = entry.relation.casefold()
    role_keys = word_form_keys(relation)
    out: List[ProfilePerson] = []
    matched = False
    for p in people:
        if not _person_matches(p, mention):
            out.append(p)
            continue
        matched = True
        if p.name:
            out.append(ProfilePerson(p.name, relation, p.name_keys, role_keys, p.from_list))
        else:
            out.append(ProfilePerson(entry.name, relation, name_keys(entry.name), role_keys, from_list=True))
    if not matched:
        out.append(ProfilePerson(entry.name, relation, name_keys(entry.name), role_keys, from_list=True))
    return out


def _apply_date(entry: DateAnswer, points: List[ProfileTimePoint],
                drops: Dict[str, int]) -> Optional[List[ProfileTimePoint]]:
    """The points after the owner dated the flagged mention: the points read from those words are replaced by the
    owner's point; None (nothing applied) when the answer does not read or no room is left under TIME_CAP."""
    point = owner_time_point(entry)
    if point is None:
        _bump(drops, "date_answer_unreadable")
        return None
    kept = [p for p in points if not _source_about(p.source, entry.mention)]
    if len(kept) >= TIME_CAP:
        _bump(drops, "date_answer_over_cap")
        return None
    return kept + [point]


def apply_answers(answers: ProfileAnswers, lineage_id: str, points: Sequence[ProfileTimePoint],
                  people: Sequence[ProfilePerson], review_needed: bool, review_items: Sequence[ProfileReviewItem],
                  answered_cities: Set[str], listed: Sequence[ConfirmedPerson], drops: Dict[str, int],
                  ) -> Tuple[Tuple[ProfileTimePoint, ...], Tuple[ProfilePerson, ...], bool,
                             Tuple[ProfileReviewItem, ...]]:
    """(points, people, review needed, review items) after the owner's answers this fact names. A `places` item whose
    city an answer placed is dropped; a `people` item with an `entry` or `not_listed` answer for this lineage and
    mention is dropped and the entry applied (an entry no longer on the list keeps the item); a `time_points` item with
    a `date` answer for this lineage and mention is dropped and its points replaced. Every dropped item counts
    `review_item_answered`; the flag clears when an item was answered and none is left. `unknown` changes nothing."""
    out_points, out_people = list(points), list(people)
    kept: List[ProfileReviewItem] = []
    answered = 0
    by_name = {entry.name.casefold(): entry for entry in listed}
    for item in review_items:
        mention = fold(item.mention)
        done = False
        if item.field == "places":
            done = mention in answered_cities
        elif item.field == "people" and lineage_id:
            person = answers.people.get(person_point_key(lineage_id, item.mention))
            if person is not None and person.resolution == "not_listed":
                done = True
            elif person is not None and person.resolution == "entry":
                entry = by_name.get(person.name.casefold())
                if entry is None:
                    _bump(drops, "person_answer_stale")
                else:
                    out_people = _apply_person(entry, mention, out_people)
                    done = True
        elif item.field == "time_points" and lineage_id:
            dated = answers.dates.get(date_point_key(lineage_id))
            if dated is not None and dated.resolution == "date" and fold(dated.mention) == mention:
                replaced = _apply_date(dated, out_points, drops)
                if replaced is not None:
                    out_points, done = replaced, True
        if done:
            answered += 1
            _bump(drops, "review_item_answered")
        else:
            kept.append(item)
    if answered and not kept:
        review_needed = False
    return tuple(out_points), tuple(out_people), review_needed, tuple(kept)


# ── The whole profile ───────────────────────────────────────────────────────

def _metadata_values(metadata: Optional[Mapping[str, Any]]) -> List[str]:
    items = metadata or {}
    return [metadata_value(items[key]) for key in sorted(items)]


def derive_profile(answer: ProfileAnswer, text: str, metadata: Optional[Mapping[str, Any]],
                   atlas: PlaceNames, recorded_year: Optional[int] = None,
                   listed: Sequence[ConfirmedPerson] = (), answers: ProfileAnswers = EMPTY_ANSWERS,
                   lineage_id: str = "") -> DeriveOutcome:
    """The profile of a non-directive fact from its validated answer (a directive takes `directive_profile`).
    `recorded_year` fills dates the fact prints without a year; `listed` are the confirmed list's entries, the only
    names kept without being printed; `answers` are the owner's answers to profile questions, applied to this fact by
    its `lineage_id` (person and date answers) and its places (§4.7)."""
    drops: Dict[str, int] = {}
    # The retraction prefix is code's, not the fact's: its date is no source of a time point.
    texts = [_RETRACTION_PREFIX.sub("", text, count=1), *metadata_lines(metadata)]
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

    points = check_time_points(answer.time_points, evidence, raw_all, values, drops, recorded_year)
    place_keys, unresolved, answered_cities = _check_places(answer.places, evidence, atlas, drops, answers)
    people = check_people(answer.people, evidence, drops, listed)
    review_needed, review_items = check_review(answer, evidence, drops)
    points, people, review_needed, review_items = apply_answers(
        answers, lineage_id, points, people, review_needed, review_items, answered_cities, listed, drops)
    profile = FactProfile(
        primary_domain=primary, domain_keys=(primary, *extras), user_identity=answer.user_identity,
        kinds=tuple(answer.kinds), sensitivity=answer.sensitivity, label_words=labels, time_role=answer.time_role,
        time_points=points, place_keys=place_keys, places_unresolved=unresolved, people=people,
        profile_answer=answer.stored(), profile_drops=drops, review_needed=review_needed, review_items=review_items,
    )
    return DeriveOutcome(profile, redo)
