"""
People cards (docs/10_rfcs/FACT_PROFILE_RFC.md §7): who the owner's facts keep mentioning, grouped by name.

Public interface:
    PERSON_CARD_MIN_FACTS = 10
    PersonCard(card_key, display, spellings, name_keys, relation, fact_count)     frozen
    PersonCardBuild(cards, ambiguous)                                            frozen
    build_person_cards(facts) -> tuple[PersonCard, ...]
    build_person_cards_counted(facts) -> PersonCardBuild

A card is a retrieval index entry: never a fact, never in a prompt, never read by the consolidator. It exists so a
search for one spelling of a person (or for a relation word) can also look for every other spelling the facts use.

The rule. A stored mention is `{name, role, name_keys, role_keys}`; its `x:` name keys are one transliteration
skeleton per word. A mention with a name and at least one skeleton counts (a role-only mention is ignored). Mentions
with the same skeleton set are one spelling group. The distinct sets are taken fullest first (size, then mention count,
then sorted): a set that fits inside exactly one existing root joins that root's group; inside none, it founds a new
group and is its root; inside two or more roots it is ambiguous (a bare surname with two relatives of that surname) and
joins nobody. Skeletons cannot join a nickname to a name: a short form with another skeleton is its own group.
A group becomes a card at PERSON_CARD_MIN_FACTS distinct facts. The module reads only `fact.id` and `fact.people`.
"""
from collections import Counter
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .drive_key_forms import primary
from .entities import FactEntity
from .fact_profile_derive import RELATION_WORDS

PERSON_CARD_MIN_FACTS = 10
_SKELETON_PREFIX = "x:"
_RELATION_PRIMARIES = frozenset(primary(word) for word in RELATION_WORDS)


@dataclass(frozen=True)
class PersonCard:
    """One person as the facts write them: `card_key` is the fullest spelling's skeleton words joined by `_` (a rebuild
    keeps it), `display` the most used spelling of that fullest form, `spellings` every distinct spelling (most used
    first), `name_keys` the union of the mentions' name keys, `relation` the relation word most of the mentions carry
    as their role (None when none does), `fact_count` the distinct facts."""
    card_key: str
    display: str
    spellings: Tuple[str, ...]
    name_keys: Tuple[str, ...]
    relation: Optional[str]
    fact_count: int


@dataclass(frozen=True)
class PersonCardBuild:
    """The cards and how many distinct skeleton sets were ambiguous (joined no group)."""
    cards: Tuple[PersonCard, ...]
    ambiguous: int


@dataclass(frozen=True)
class _Mention:
    fact_id: str
    name: str
    skeleton: FrozenSet[str]
    keys: Tuple[str, ...]
    role: Optional[str]


def build_person_cards(facts: Sequence[FactEntity]) -> Tuple[PersonCard, ...]:
    """The cards of the given current facts (see the module docstring); most facts first, then by key."""
    return build_person_cards_counted(facts).cards


def build_person_cards_counted(facts: Sequence[FactEntity]) -> PersonCardBuild:
    mentions = _mentions(facts)
    by_set: Dict[FrozenSet[str], List[_Mention]] = {}
    for mention in mentions:
        by_set.setdefault(mention.skeleton, []).append(mention)
    order = sorted(by_set, key=lambda s: (-len(s), -len(by_set[s]), sorted(s)))

    groups: Dict[FrozenSet[str], List[_Mention]] = {}
    ambiguous = 0
    for skeleton in order:
        homes = [root for root in groups if skeleton <= root]
        if not homes:
            groups[skeleton] = list(by_set[skeleton])
        elif len(homes) == 1:
            groups[homes[0]].extend(by_set[skeleton])
        else:
            ambiguous += 1

    cards = [_card(root, members, by_set[root]) for root, members in groups.items()]
    kept = [card for card in cards if card.fact_count >= PERSON_CARD_MIN_FACTS]
    kept.sort(key=lambda card: (-card.fact_count, card.card_key))
    return PersonCardBuild(tuple(kept), ambiguous)


def _mentions(facts: Sequence[FactEntity]) -> List[_Mention]:
    found: List[_Mention] = []
    for fact in facts:
        for person in fact.people or ():
            name = " ".join(str(person.get("name") or "").split())
            keys = tuple(str(k) for k in person.get("name_keys") or ())
            skeleton = frozenset(k[len(_SKELETON_PREFIX):] for k in keys if k.startswith(_SKELETON_PREFIX))
            if not name or not skeleton:
                continue
            role = " ".join(str(person.get("role") or "").split()).casefold() or None
            found.append(_Mention(fact.id, name, skeleton, keys, role))
    return found


def _card(root: FrozenSet[str], members: List[_Mention], own: List[_Mention]) -> PersonCard:
    names = Counter(m.name for m in members)
    own_names = Counter(m.name for m in own)
    display = sorted(own_names, key=lambda n: (-own_names[n], n))[0]
    return PersonCard(
        card_key="_".join(sorted(root)),
        display=display,
        spellings=tuple(sorted(names, key=lambda n: (-names[n], n))),
        name_keys=tuple(sorted({key for m in members for key in m.keys})),
        relation=_relation(members),
        fact_count=len({m.fact_id for m in members}),
    )


def _relation(members: List[_Mention]) -> Optional[str]:
    """The relation primary carried as the whole role by more than half of the mentions, the most common one."""
    seen = Counter(
        role for role in (_relation_of(m.role) for m in members) if role is not None)
    if not seen:
        return None
    best, count = sorted(seen.items(), key=lambda item: (-item[1], item[0]))[0]
    return best if count * 2 > len(members) else None


def _relation_of(role: Optional[str]) -> Optional[str]:
    if not role or len(role.split()) != 1:
        return None
    form = primary(role)
    return form if form in _RELATION_PRIMARIES else None
