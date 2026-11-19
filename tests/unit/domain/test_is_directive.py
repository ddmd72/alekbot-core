"""`is_directive` (fact profile RFC §6 row 15): the one predicate that decides "standing directive", reading only the
consolidator's `directive` flag (PR 13; no fallback to the old `domain` marker, which an owner-run mark script turns
into the flag before the deploy).

Before it, four kinds of site decided it on their own:
- the prompt builder's split of the cached biographical dicts (the cached fact's marker);
- the profile step's private `_is_directive` (the fact's marker);
- the repository reads of the companion and Stage 2b: now `get_active_facts_ordered(directive_only=True)`, the
  condition on the same flag the predicate reads;
- `expiry_pinned`: directive flag, principle or `mindset` tag. Principles and mindset facts are pinned but are not
  directives (no former directive reader treated them as one); the pin keeps its own rule over the same flag.
One case per former rule below."""
from typing import Any, Dict

import pytest

from src.domain.entities import FactType
from src.domain.fact_domains import Domain
from src.domain.fact_lifecycle import expiry_pinned, is_directive
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_lifecycle_world import make_fact_world

DIRECTIVE_TEXT = "suggest only vegetarian recipes"


def _cached(text: str, **over: Any) -> Dict[str, Any]:
    """A fact as the biographical cache holds it (the prompt builder's input): plain values."""
    return {"text": text, "tags": [], **over}


# ------------------------------------------------------------------------------------------------ the flag

def test_a_fact_with_the_directive_flag_is_a_directive():
    assert is_directive(make_fact(DIRECTIVE_TEXT, directive=True)) is True


def test_an_ordinary_fact_is_not_a_directive():
    assert is_directive(make_fact("plays tennis on Sundays", directive=False)) is False


def test_a_fact_with_no_flag_is_not_a_directive():
    assert is_directive(make_fact("morning yoga 30 min")) is False


def test_the_profile_domain_does_not_make_a_directive_only_the_flag_does():
    fact = make_fact("morning yoga 30 min", primary_domain=Domain.AGENT_DIRECTIVES,
                     domain_keys=[Domain.AGENT_DIRECTIVES])
    assert is_directive(fact) is False


def test_the_old_domain_marker_is_no_longer_read():
    """No fallback (owner decision 1): a stored document that still carries the old directive domain and no flag
    loads as not a directive, as a fact and as a cached dict."""
    stored = make_fact(DIRECTIVE_TEXT, domain="agent_directive")   # the old key loads (ignored), no flag
    assert is_directive(stored) is False
    assert is_directive(_cached(DIRECTIVE_TEXT, domain="agent_directive")) is False
    assert is_directive(_cached(DIRECTIVE_TEXT, directive=True)) is True


# ------------------------------------------------------------------------------- parity with the former sites

@pytest.mark.parametrize("cached, expected", [
    (_cached(DIRECTIVE_TEXT, directive=True), True),
    (_cached("plays tennis on Sundays", directive=False), False),
    (_cached("born 1980-01-01 in Springfield", domain="agent_directive"), False),  # the old marker alone
    (_cached("morning yoga 30 min"), False),                                     # no flag in the dict
    (_cached("answer in English", tags=["mindset"]), False),                     # the split never looked at tags
    (_cached(DIRECTIVE_TEXT, directive="true"), False),                          # only a stored true counts
])
def test_prompt_builder_split_on_cached_dicts(cached, expected):
    """The split of the cached biographical dicts: the flag a cached fact carries."""
    assert is_directive(cached) is (cached.get("directive") is True) is expected


@pytest.mark.parametrize("over, expected", [
    ({"directive": True}, True),
    ({"directive": False, "type": FactType.PRINCIPLE}, False),
    ({"directive": False, "tags": ["mindset"]}, False),
    ({"primary_domain": Domain.AGENT_DIRECTIVES}, False),
])
def test_profile_step_parity(over, expected):
    """Former rule (`fact_profile_service._is_directive`): the fact's marker, now its flag."""
    fact = make_fact(DIRECTIVE_TEXT, **over)
    assert is_directive(fact) is (fact.directive is True) is expected


async def test_repository_read_parity_selects_exactly_the_facts_the_predicate_accepts():
    """Former rule (companion directives, Stage 2b review and cap): the read by the directive marker. The read now
    filters on the flag (`directive_only`); what it returns is what the predicate accepts among the current facts."""
    _, repo, _ = make_fact_world()
    facts = [
        make_fact("suggest only vegetarian recipes", directive=True),
        make_fact("answer in English", directive=True, age_days=1),
        make_fact("plays tennis on Sundays", type=FactType.PRINCIPLE),
        make_fact("morning yoga 30 min", tags=["mindset"]),
        make_fact("Alex's car insurance with Example Mutual Insurance", primary_domain=Domain.AGENT_DIRECTIVES),
    ]
    for fact in facts:
        await repo.add_fact(fact)

    read = await repo.get_active_facts_ordered(ACC, directive_only=True)

    assert {f.text for f in read} == {f.text for f in facts if is_directive(f)} == {
        "suggest only vegetarian recipes", "answer in English"}


@pytest.mark.parametrize("over", [
    {"directive": True},
    {"type": FactType.PRINCIPLE},
    {"tags": ["Mindset"]},
    {"directive": False},
    {"type": FactType.STATE},
])
def test_expiry_pin_parity_is_the_directive_or_a_principle_or_a_mindset_fact(over):
    """Former rule (`expiry_pinned`): directive marker, principle or the `mindset` tag. Its directive leg is the
    predicate's flag; the other two legs stay the pin's own."""
    fact = make_fact(DIRECTIVE_TEXT, **over)
    expected = (is_directive(fact) or fact.type == FactType.PRINCIPLE
                or "mindset" in {t.lower() for t in fact.tags})
    assert expiry_pinned(fact.directive, fact.type, fact.tags) is expected


def test_a_principle_and_a_mindset_fact_are_pinned_but_not_directives():
    principle = make_fact("suggest only vegetarian recipes", type=FactType.PRINCIPLE)
    mindset = make_fact("answer in English", tags=["mindset"])
    for fact in (principle, mindset):
        assert expiry_pinned(fact.directive, fact.type, fact.tags) is True and is_directive(fact) is False
