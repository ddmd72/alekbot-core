"""
Email card people — whom a mail is about: the printed-person check, the `about` derivation and the people keys
(email index v2 §2.3, §3.1; card slice 1). Pure: no I/O.

The writer sees the owner and the confirmed people list, each entry under a per-render handle `P<n>` (`person_handles`:
the static block that renders the list must number it with this function, so a returned handle maps back here). It
names each person other than the owner whose matter the mail is (`printed`: a name as printed, or the relation words
when no name is; a handle when exactly one entry fits) and answers whether the owner is one of the subjects (`about`).

**Printed-person check.** A person is kept only when its printed form is in the mail (as written, or every word in some
form); the owner named again (and no listed person with them) is not a person. A kept person stays linked to its entry only when what is printed names
that entry (one of its name forms, or the other script's spelling of one) or its printed relation words point to that
entry alone; otherwise the link is dropped and the person is kept as a printed name. A handle not on the rendered list
links nothing. Never is a name or relation carried from the list into the card unless the mail prints it.

**`about`, derived** from the surviving linked people and the model's owner answer (owner a subject: `owner` or
`household`): `household` when two or more linked people survive, or the owner is a subject and at least one does;
`person` when exactly one survives and the owner is not a subject; `owner` when the owner is a subject and none
survives; `unknown` otherwise (a printed stranger alone is `unknown`). The owner's part is the model's (how a mail
addresses its reader is not a printed fact); the people count is code's.

**People keys** (this slice): the printed name forms only — the `e:` and skeleton keys of each kept person's printed
name (`phrase_keys`); a person kept by relation words alone has no name printed and no key. Stable `p:<entry id>` keys
come with the confirmed list's entry ids (a later slice): `people_keys` is the one place they are added.

Public interface:
    person_handles(people) -> {"P1": ConfirmedPerson, ...}
    resolve_people(answer_people, model_about, people, evidence) -> ((CardPerson, ...), About)
    derive_about(model_about, linked) -> About
    people_keys(persons) -> (key, ...)
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from .confirmed_people import ConfirmedPeople, ConfirmedPerson, _relation_words
from .drive_enrichment import Grade
from .drive_grade import TextEvidence
from .drive_keys import phrase_keys
from .email_card import About, AnswerPerson, CardPerson

LINKED_BY_NAME = "name"
LINKED_BY_RELATION = "relation"
_OWNER_SUBJECT = frozenset({About.OWNER, About.HOUSEHOLD})


def person_handles(people: Optional[ConfirmedPeople]) -> Dict[str, ConfirmedPerson]:
    """The per-render handles of the confirmed people (the owner excluded): `P1` is the list's first person."""
    if people is None:
        return {}
    return {f"P{i}": entry for i, entry in enumerate(people.people, 1)}


def resolve_people(answer_people: Sequence[AnswerPerson], model_about: About, people: Optional[ConfirmedPeople],
                   evidence: TextEvidence) -> Tuple[Tuple[CardPerson, ...], About]:
    """The people the card keeps, in the answer's order (each entry and each printed form once), and the derived
    `about` (module docstring)."""
    handles = person_handles(people)
    entries = list(handles.values())
    kept: List[CardPerson] = []
    seen_printed: Set[str] = set()
    seen_entries: Set[str] = set()
    for person in answer_people:
        printed = " ".join(person.printed.split())
        if not printed or printed.casefold() in seen_printed or not _printed_in(printed, evidence):
            continue
        if people is not None and _is_owner(printed, people.user, entries):
            continue
        entry = handles.get(person.handle)
        linked_by = ""
        if entry is not None:
            if _names_entry(printed, entry):
                linked_by = LINKED_BY_NAME
            elif _relation_entries(printed, entries) == [entry]:
                linked_by = LINKED_BY_RELATION
        if linked_by and entry is not None:
            if entry.name in seen_entries:
                continue
            seen_entries.add(entry.name)
            kept.append(CardPerson(printed=printed, entry_name=entry.name, linked_by=linked_by))
        else:
            kept.append(CardPerson(printed=printed))
        seen_printed.add(printed.casefold())
    persons = tuple(kept)
    return persons, derive_about(model_about, persons)


def derive_about(model_about: About, persons: Sequence[CardPerson]) -> About:
    """The stored `about` from the linked people count and the model's owner answer (module docstring)."""
    linked = len({p.entry_name for p in persons if p.entry_name})
    owner = model_about in _OWNER_SUBJECT
    if linked >= 2 or (owner and linked >= 1):
        return About.HOUSEHOLD
    if linked == 1:
        return About.PERSON
    if owner:
        return About.OWNER
    return About.UNKNOWN


def people_keys(persons: Sequence[CardPerson]) -> Tuple[str, ...]:
    """The card's people keys: each kept person's printed name forms (module docstring). The seam for `p:` keys."""
    keys: Dict[str, None] = {}
    for person in persons:
        if person.linked_by == LINKED_BY_RELATION:
            continue
        keys.update(dict.fromkeys(phrase_keys(person.printed)))
    return tuple(keys)


# -- implementation -------------------------------------------------------------------------------------------------

def _printed_in(printed: str, evidence: TextEvidence) -> bool:
    """The form is in the mail as written, or each of its words in some form ("Doe, Jamie" for "Jamie Doe")."""
    return evidence.occurs(printed) or evidence.words_grade(printed) is not Grade.NOT_IN_TEXT


def _names_entry(printed: str, entry: ConfirmedPerson) -> bool:
    """What is printed holds one of the entry's name forms, or the other script's spelling of one."""
    own = TextEvidence([printed])
    for form in (entry.name, *entry.forms):
        if own.occurs(form) or own.skeleton_seen(form):
            return True
    return False


def _is_owner(printed: str, owner: ConfirmedPerson, entries: Sequence[ConfirmedPerson]) -> bool:
    """The form names the owner and no listed person (a surname the family shares names them all)."""
    return _names_entry(printed, owner) and not any(_names_entry(printed, e) for e in entries)


def _relation_entries(printed: str, entries: Sequence[ConfirmedPerson]) -> List[ConfirmedPerson]:
    """The entries whose relation words the printed form holds (plural and possessive forms count)."""
    own = TextEvidence([printed])
    out: List[ConfirmedPerson] = []
    for entry in entries:
        if any(own.words_grade(word) is not Grade.NOT_IN_TEXT for word in _relation_words(entry.relation)):
            out.append(entry)
    return out
