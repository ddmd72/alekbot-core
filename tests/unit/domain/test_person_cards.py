"""People cards (fact profile RFC §7): the grouping rule of `build_person_cards`, on the invented universe (Maria Doe,
Alex's mother; Peter Doe, Alex's father; Clara Moreira, Alex's partner). Mentions are built with the real `name_keys`."""
import pytest

from src.domain.fact_profile_derive import name_keys
from src.domain.person_cards import PERSON_CARD_MIN_FACTS, PersonCard, build_person_cards, build_person_cards_counted
from tests.support.fact_kit import make_fact


def mention(name: str, role: str = ""):
    return {"name": name, "role": role, "name_keys": list(name_keys(name)), "role_keys": []}


def fact(index: int, *people, tag: str = "f"):
    return make_fact(f"{tag} {index}", id=f"{tag}-{index}", people=list(people))


def facts_of(count: int, *people, start: int = 0, tag: str = "f"):
    return [fact(i, *people, tag=tag) for i in range(start, start + count)]


def only(cards):
    (card,) = cards
    return card


def test_maria_written_four_ways_is_one_card_and_the_short_form_stays_apart():
    facts = (facts_of(4, mention("Maria Doe", "mother"), tag="a")
             + facts_of(3, mention("Maria A. Doe", "mother"), tag="b")
             + facts_of(2, mention("Mariia"), tag="c")
             + facts_of(2, mention("Марія"), tag="d")
             + facts_of(12, mention("Mia"), tag="e"))

    cards = build_person_cards(facts)

    by_key = {card.card_key: card for card in cards}
    maria = by_key["doe_mari"]
    assert maria.fact_count == 11
    assert set(maria.spellings) == {"Maria Doe", "Maria A. Doe", "Mariia", "Марія"}
    assert maria.display == "Maria Doe"
    assert "mia" in by_key and by_key["mia"].spellings == ("Mia",) and by_key["mia"].fact_count == 12


def test_a_bare_surname_with_two_relatives_of_that_name_is_ambiguous_and_counted():
    facts = (facts_of(10, mention("Maria Doe"), tag="a") + facts_of(10, mention("Peter Doe"), tag="b")
             + facts_of(5, mention("Doe"), tag="c"))

    build = build_person_cards_counted(facts)

    assert sorted(card.card_key for card in build.cards) == ["doe_mari", "doe_peter"]
    assert all(card.fact_count == 10 for card in build.cards)     # the lone "Doe" facts joined nobody
    assert build.ambiguous == 1


def test_a_bare_surname_with_one_relative_joins_her():
    facts = facts_of(8, mention("Maria Doe"), tag="a") + facts_of(3, mention("Doe"), tag="b")
    card = only(build_person_cards(facts))
    assert (card.card_key, card.fact_count, card.spellings) == ("doe_mari", 11, ("Maria Doe", "Doe"))


def test_nine_distinct_facts_make_no_card_and_ten_do():
    assert PERSON_CARD_MIN_FACTS == 10
    assert build_person_cards(facts_of(9, mention("Maria Doe"))) == ()
    assert only(build_person_cards(facts_of(10, mention("Maria Doe")))).fact_count == 10


def test_two_mentions_in_one_fact_count_once():
    twice = [fact(i, mention("Maria Doe"), mention("Mariia")) for i in range(9)]
    assert build_person_cards(twice) == ()
    card = only(build_person_cards(twice + [fact(9, mention("Maria Doe"))]))
    assert card.fact_count == 10


def test_the_relation_is_the_majority_role_and_none_on_a_tie_or_a_minority():
    def card_of(*roles: str):
        people = [mention("Maria Doe", role) for role in roles]
        return only(build_person_cards([fact(i, people[i % len(people)]) for i in range(10)]))

    assert card_of("mother", "mother", "mother", "").relation == "mother"
    assert card_of("mother", "father").relation is None                   # a tie: half is not more than half
    assert card_of("mother", "", "", "").relation is None                 # a minority
    assert card_of("mom", "mother", "mother").relation == "mother"        # no synonym folding: mom is its own word
    assert card_of("Mother", "mother").relation == "mother"               # casefolded
    assert card_of("my mother", "my mother").relation is None             # more than one token is no relation word
    assert card_of("neighbour", "neighbour").relation is None             # not in the closed list


def test_a_partner_card_carries_partner():
    card = only(build_person_cards(facts_of(10, mention("Clara Moreira", "partner"))))
    assert (card.display, card.relation) == ("Clara Moreira", "partner")


def test_role_only_and_nameless_mentions_are_ignored():
    people = [{"name": "", "role": "mother", "name_keys": [], "role_keys": ["w:mother"]},
              {"name": "A.", "role": "", "name_keys": [], "role_keys": []}]
    assert build_person_cards([fact(i, *people) for i in range(12)]) == ()


def test_the_key_stays_when_a_fuller_spelling_is_added():
    before = build_person_cards(facts_of(10, mention("Maria Doe"), tag="a"))
    after = build_person_cards(facts_of(10, mention("Maria Doe"), tag="a") + facts_of(3, mention("Maria A. Doe"), tag="b"))
    assert only(before).card_key == only(after).card_key == "doe_mari"
    assert only(after).fact_count == 13


def test_display_is_the_roots_most_used_spelling_and_spellings_go_by_use_then_alphabet():
    facts = (facts_of(3, mention("Maria  Doe"), tag="a")                    # whitespace collapsed
             + facts_of(2, mention("Maria DOE"), tag="b")
             + facts_of(2, mention("Mariia"), tag="c") + facts_of(2, mention("Maria"), tag="d")
             + facts_of(3, mention("Марія"), tag="e"))
    card = only(build_person_cards(facts))
    assert card.display == "Maria Doe"
    assert card.spellings == ("Maria Doe", "Марія", "Maria", "Maria DOE", "Mariia")
    assert list(card.name_keys) == sorted(set(card.name_keys)) and "x:mari" in card.name_keys


def test_cards_come_most_facts_first_then_by_key():
    facts = facts_of(10, mention("Clara Moreira"), tag="a") + facts_of(12, mention("Maria Doe"), tag="b")
    assert [card.card_key for card in build_person_cards(facts)] == ["doe_mari", "klar_moreir"]


def test_no_facts_and_facts_without_people_give_nothing():
    assert build_person_cards([]) == ()
    assert build_person_cards([make_fact("no people"), make_fact("empty", people=[])]) == ()
    assert build_person_cards_counted([]).ambiguous == 0


def test_a_card_is_frozen():
    card = PersonCard("k", "K", ("K",), (), None, 10)
    with pytest.raises(Exception):
        card.display = "other"   # type: ignore[misc]
