"""The owner-confirmed people list (FACT_PROFILE_RFC §4.5): the user and the people in their life, each with the other
forms the name is written in and the relation to the user.

A fact often names a relative by a first name alone; the model cannot know who that is, the owner can. The list is
written only from the owner's confirmation, sent whole to the profiler before the fact (`render_people_block`), and
folded into a fact's fingerprint only through the entries the fact names (`people_named_in_texts`), so a list edit
re-profiles only the facts that mention the edited person by a name form or by their relation word.

Each person (never the user) carries a stable `entry_id` assigned by `assign_entry_ids`, never rendered and never part
of a fingerprint or digest: it survives a reorder, a re-spelling and a rename through a shared form, and is never reused
(EMAIL_INDEX_V2 RFC §2.3).

`ConfirmedPerson`, `ConfirmedPeople` and `EntryIdPlan` are one value shape and its id assignment and share this module
on purpose.
"""
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .drive_enrichment import Grade
from .drive_grade import TextEvidence
from .drive_key_forms import token_spans

CONFIRMED_SOURCES = ("owner_script", "review_answer")
PEOPLE_BLOCK_TITLE = "Confirmed people (the first entry is the user):"
USER_RELATION = "the user"
_MIN_WORD_LETTERS = 2
_MIN_RELATION_LETTERS = 3
# Words of a relation that qualify it and do not name it: "older daughter" is found by "daughter".
_RELATION_STOP = frozenset({"the", "of", "in", "and", "law", "older", "younger", "elder", "eldest", "youngest",
                            "step", "half", "great"})
_RELATION = re.compile(r"[^\W\d_]+(?:[ -][^\W\d_]+)*")


@dataclass(frozen=True)
class ConfirmedPerson:
    name: str                    # the full name as the owner wrote it
    forms: Tuple[str, ...]       # other forms, owner's order, deduplicated, never the name itself
    relation: str                # plain lowercase English; "" only for the user entry
    is_user: bool = False
    entry_id: Optional[int] = field(default=None, compare=False)   # positive; people only; assigned, not typed


@dataclass(frozen=True)
class ConfirmedPeople:
    user: ConfirmedPerson
    people: Tuple[ConfirmedPerson, ...]
    next_entry_id: Optional[int] = field(default=None, compare=False)   # the counter: above every id ever assigned

    def entries(self) -> Tuple[ConfirmedPerson, ...]:
        """The user first, then the people in the owner's order."""
        return (self.user, *self.people)


def _clean(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    return " ".join(value.split())


def _forms(raw: Any, name: str, what: str) -> Tuple[str, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{what} forms must be a list")
    seen = {name.casefold()}
    forms: List[str] = []
    for item in raw:
        form = _clean(item, f"{what} form")
        if not form:
            raise ValueError(f"{what} has an empty form")
        if form.casefold() not in seen:
            seen.add(form.casefold())
            forms.append(form)
    return tuple(forms)


def _entry(raw: Any, what: str, is_user: bool) -> ConfirmedPerson:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{what} must be a mapping")
    name = _clean(raw.get("name"), f"{what} name")
    if not name:
        raise ValueError(f"{what} has no name")
    forms = _forms(raw.get("forms"), name, what)
    if is_user:
        return ConfirmedPerson(name, forms, "", True)
    relation = _clean(raw.get("relation"), f"{what} relation")
    if not relation:
        raise ValueError(f"{what} has no relation")
    if relation != relation.lower() or not _RELATION.fullmatch(relation):
        raise ValueError(f"{what} relation must be lowercase letters and spaces")
    return ConfirmedPerson(name, forms, relation, entry_id=_id(raw.get("entry_id"), f"{what} entry_id"))


def _id(value: Any, what: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{what} must be a positive integer")
    return value


def parse_confirmed_people(data: Mapping[str, Any]) -> ConfirmedPeople:
    """The list from its stored or typed shape `{user: {name, forms}, people: [{name, forms, relation}]}`; ValueError
    on any broken rule (empty name or relation, a relation that is not lowercase letters, a name used twice)."""
    if not isinstance(data, Mapping):
        raise ValueError("the confirmed people list must be a mapping")
    user = _entry(data.get("user"), "user", True)
    raw_people = data.get("people")
    if raw_people is None:
        raw_people = []
    if not isinstance(raw_people, (list, tuple)):
        raise ValueError("people must be a list")
    people = tuple(_entry(raw, f"person {i + 1}", False) for i, raw in enumerate(raw_people))
    names = [entry.name.casefold() for entry in (user, *people)]
    if len(set(names)) != len(names):
        raise ValueError("two entries have the same name")
    counter = _id(data.get("next_entry_id"), "next_entry_id")
    ids = [p.entry_id for p in people if p.entry_id is not None]
    if len(set(ids)) != len(ids):
        raise ValueError("two people have the same entry_id")
    if counter is not None and any(i >= counter for i in ids):
        raise ValueError("an entry_id is not below next_entry_id")
    return ConfirmedPeople(user, people, counter)


def confirmed_people_document(people: ConfirmedPeople) -> Dict[str, Any]:
    """`{user, people}` in the shape `parse_confirmed_people` reads; the adapter adds account, source and time. A
    person's `entry_id` and the top-level `next_entry_id` are written only when set."""
    document: Dict[str, Any] = {
        "user": {"name": people.user.name, "forms": list(people.user.forms)},
        "people": [{"name": p.name, "forms": list(p.forms), "relation": p.relation,
                    **({"entry_id": p.entry_id} if p.entry_id is not None else {})} for p in people.people],
    }
    if people.next_entry_id is not None:
        document["next_entry_id"] = people.next_entry_id
    return document


@dataclass(frozen=True)
class EntryIdPlan:
    """What `assign_entry_ids` did: names with their ids, for the owner's dry run."""
    kept: Tuple[Tuple[str, int], ...]              # (name, id): the same casefolded name
    renamed: Tuple[Tuple[str, str, int], ...]      # (stored name, new name, id): kept through a shared form
    new: Tuple[Tuple[str, int], ...]
    retired: Tuple[Tuple[str, int], ...]           # stored ids no new entry took; never reused


def _spellings(person: ConfirmedPerson) -> frozenset:
    return frozenset(s.casefold() for s in (person.name, *person.forms))


def assign_entry_ids(new: ConfirmedPeople, stored: Optional[ConfirmedPeople]) -> Tuple[ConfirmedPeople, EntryIdPlan]:
    """`new` with an `entry_id` on every person (the user never has one) and the advanced counter. Ids on `new` are
    ignored. A person takes the id of the stored person with the same casefolded name; failing that, of the one stored
    person that still has an id and shares a form or name with them, provided no other unmatched new person shares one
    with that stored person (a rename). Anyone else takes the next id from the counter, in the owner's order. The
    counter only grows: `max(stored counter, highest stored id + 1)`, so a retired id is never reused. Idempotent:
    `assign_entry_ids(x, x)` keeps every id; a stored list without ids gets 1..n."""
    old = [p for p in (stored.people if stored else ()) if p.entry_id is not None]
    by_name = {p.name.casefold(): p for p in old}
    taken: Dict[int, Tuple[str, int]] = {}      # position in new.people -> (stored name, id)
    for i, person in enumerate(new.people):
        match = by_name.get(person.name.casefold())
        if match is not None:
            taken[i] = (match.name, match.entry_id or 0)
    claimed = {entry_id for _, entry_id in taken.values()}
    free_old = [p for p in old if p.entry_id not in claimed]
    free_new = [i for i in range(len(new.people)) if i not in taken]
    renamed: Dict[int, Tuple[str, int]] = {}
    for i in free_new:
        mine = _spellings(new.people[i])
        fits = [p for p in free_old if mine & _spellings(p)]
        if len(fits) != 1:
            continue
        rivals = [j for j in free_new if _spellings(new.people[j]) & _spellings(fits[0])]
        if rivals == [i]:
            renamed[i] = (fits[0].name, fits[0].entry_id or 0)
    counter = max((stored.next_entry_id if stored else None) or 1, max((p.entry_id or 0 for p in old), default=0) + 1)
    people: List[ConfirmedPerson] = []
    kept: List[Tuple[str, int]] = []
    renamed_out: List[Tuple[str, str, int]] = []
    fresh: List[Tuple[str, int]] = []
    for i, person in enumerate(new.people):
        if i in taken:
            entry_id = taken[i][1]
            kept.append((person.name, entry_id))
        elif i in renamed:
            entry_id = renamed[i][1]
            renamed_out.append((renamed[i][0], person.name, entry_id))
        else:
            entry_id, counter = counter, counter + 1
            fresh.append((person.name, entry_id))
        people.append(replace(person, entry_id=entry_id))
    used = {p.entry_id for p in people}
    retired = tuple((p.name, p.entry_id or 0) for p in old if p.entry_id not in used)
    result = ConfirmedPeople(replace(new.user, entry_id=None), tuple(people), counter)
    return result, EntryIdPlan(tuple(kept), tuple(renamed_out), tuple(fresh), retired)


def _words(*spellings: str) -> List[str]:
    return [token for spelling in spellings for token, _, _ in token_spans(spelling)
            if len(token) >= _MIN_WORD_LETTERS]


def _relation_words(relation: str) -> List[str]:
    return [t for t in _words(relation) if len(t) >= _MIN_RELATION_LETTERS and t.casefold() not in _RELATION_STOP]


def _names_seen(entry: ConfirmedPerson, evidence: TextEvidence) -> bool:
    return any(evidence.occurs(word) or bool(evidence.skeleton_seen(word))
               for word in _words(entry.name, *entry.forms))


def _relation_seen(entry: ConfirmedPerson, evidence: TextEvidence) -> bool:
    if entry.is_user:
        return False
    return any(evidence.words_grade(word) is not Grade.NOT_IN_TEXT for word in _relation_words(entry.relation))


def people_named_in_texts(people: Optional[ConfirmedPeople], texts: Sequence[str]) -> Tuple[ConfirmedPerson, ...]:
    """The entries `texts` name, in list order with the user first: an entry is named when a word of its name or of one
    of its forms occurs in the texts (or its transliteration skeleton in the other script), or a word of its relation
    does (plural and possessive forms count; the user's relation is not a word to look for). Over-inclusion is
    intended: a shared surname or relation selects every entry carrying it. `None` gives nothing."""
    if people is None:
        return ()
    evidence = TextEvidence(list(texts))
    return tuple(entry for entry in people.entries()
                 if _names_seen(entry, evidence) or _relation_seen(entry, evidence))


def render_people_block(entries: Sequence[ConfirmedPerson]) -> str:
    """The block before `Fact:`: a title line, one line per entry (`- name (also written: form, form) — relation`;
    the user's relation is `the user`), then one blank line. `""` for no entries."""
    if not entries:
        return ""
    lines = [PEOPLE_BLOCK_TITLE]
    for entry in entries:
        also = f" (also written: {', '.join(entry.forms)})" if entry.forms else ""
        lines.append(f"- {entry.name}{also} — {USER_RELATION if entry.is_user else entry.relation}")
    return "\n".join(lines) + "\n\n"
