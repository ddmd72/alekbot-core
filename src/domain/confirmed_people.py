"""The owner-confirmed people list (FACT_PROFILE_RFC §4.5): the user and the people in their life, each with the other
forms the name is written in and the relation to the user.

A fact often names a relative by a first name alone; the model cannot know who that is, the owner can. The list is
written only from the owner's confirmation, sent whole to the profiler before the fact (`render_people_block`), and
folded into a fact's fingerprint only through the entries the fact names (`people_named_in_texts`), so a list edit
re-profiles only the facts that mention the edited person by a name form or by their relation word.

`ConfirmedPerson` and `ConfirmedPeople` are one value shape and share this module on purpose.
"""
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ConfirmedPeople:
    user: ConfirmedPerson
    people: Tuple[ConfirmedPerson, ...]

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
    return ConfirmedPerson(name, forms, relation)


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
    return ConfirmedPeople(user, people)


def confirmed_people_document(people: ConfirmedPeople) -> Dict[str, Any]:
    """`{user, people}` in the shape `parse_confirmed_people` reads; the adapter adds account, source and time."""
    return {
        "user": {"name": people.user.name, "forms": list(people.user.forms)},
        "people": [{"name": p.name, "forms": list(p.forms), "relation": p.relation} for p in people.people],
    }


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
