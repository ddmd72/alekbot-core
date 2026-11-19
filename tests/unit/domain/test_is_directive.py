"""`is_directive` (fact profile RFC §6 row 15): the one predicate that decides "standing directive", reading the old
`domain` marker until the consolidator's flag replaces it (PR 13).

Before it, four kinds of site decided it on their own:
- the prompt builder's split of the cached biographical dicts: `dict["domain"] == "agent_directive"`;
- the profile step's private `_is_directive`: `fact.domain == FactDomain.AGENT_DIRECTIVE`;
- the repository reads of the companion and Stage 2b: `get_active_facts_ordered(domain="agent_directive")`, now
  `domain=DIRECTIVE_DOMAIN`, the condition defined next to the predicate;
- `expiry_pinned`: directive marker, principle or `mindset` tag. Principles and mindset facts are pinned but are not
  directives (no former directive reader treated them as one); the pin keeps its own rule over the same marker.
One case per former rule below."""
from typing import Any, Dict

import pytest

from src.domain.entities import FactDomain, FactType
from src.domain.fact_domains import Domain
from src.domain.fact_lifecycle import DIRECTIVE_DOMAIN, expiry_pinned, is_directive
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_lifecycle_world import make_fact_world

DIRECTIVE_TEXT = "suggest only vegetarian recipes"


def _cached(text: str, **over: Any) -> Dict[str, Any]:
    """A fact as the biographical cache holds it (the prompt builder's input): plain values."""
    return {"text": text, "tags": [], **over}


# ------------------------------------------------------------------------------------------------ the marker

def test_a_fact_with_the_directive_domain_is_a_directive():
    assert is_directive(make_fact(DIRECTIVE_TEXT, domain=FactDomain.AGENT_DIRECTIVE)) is True


def test_an_ordinary_fact_is_not_a_directive():
    assert is_directive(make_fact("plays tennis on Sundays", domain=FactDomain.PREFERENCE)) is False


def test_a_fact_with_no_domain_is_not_a_directive():
    assert is_directive(make_fact("morning yoga 30 min", domain=None)) is False


def test_the_profile_domain_does_not_make_a_directive_until_the_flag_replaces_the_marker():
    fact = make_fact("morning yoga 30 min", domain=FactDomain.PREFERENCE, primary_domain=Domain.AGENT_DIRECTIVES,
                     domain_keys=[Domain.AGENT_DIRECTIVES])
    assert is_directive(fact) is False


def test_the_repository_condition_is_the_marker_the_predicate_reads():
    assert DIRECTIVE_DOMAIN == FactDomain.AGENT_DIRECTIVE.value
    assert is_directive(_cached(DIRECTIVE_TEXT, domain=DIRECTIVE_DOMAIN)) is True


# ------------------------------------------------------------------------------- parity with the former sites

@pytest.mark.parametrize("cached, expected", [
    (_cached(DIRECTIVE_TEXT, domain="agent_directive"), True),
    (_cached("plays tennis on Sundays", domain="preference"), False),
    (_cached("born 1980-01-01 in Springfield", domain="biographical"), False),
    (_cached("morning yoga 30 min"), False),                                   # no domain key in the dict
    (_cached("answer in English", domain="", tags=["mindset"]), False),        # the split never looked at tags
])
def test_prompt_builder_split_parity_on_cached_dicts(cached, expected):
    """Former rule: `f.get("domain", "") == FactDomain.AGENT_DIRECTIVE.value` for a dict."""
    assert is_directive(cached) is (cached.get("domain", "") == FactDomain.AGENT_DIRECTIVE.value) is expected


@pytest.mark.parametrize("over, expected", [
    ({"domain": FactDomain.AGENT_DIRECTIVE}, True),
    ({"domain": FactDomain.PREFERENCE, "type": FactType.PRINCIPLE}, False),
    ({"domain": FactDomain.PREFERENCE, "tags": ["mindset"]}, False),
    ({"domain": FactDomain.FINANCE}, False),
])
def test_profile_step_parity(over, expected):
    """Former rule (`fact_profile_service._is_directive`): `fact.domain == FactDomain.AGENT_DIRECTIVE`."""
    fact = make_fact(DIRECTIVE_TEXT, **over)
    assert is_directive(fact) is (fact.domain == FactDomain.AGENT_DIRECTIVE) is expected


async def test_repository_read_parity_selects_exactly_the_facts_the_predicate_accepts():
    """Former rule (companion directives, Stage 2b review and cap): the read by the directive domain. The read now
    filters on `DIRECTIVE_DOMAIN`; what it returns is what the predicate accepts among the current facts."""
    _, repo, _ = make_fact_world()
    facts = [
        make_fact("suggest only vegetarian recipes", domain=FactDomain.AGENT_DIRECTIVE),
        make_fact("answer in English", domain=FactDomain.AGENT_DIRECTIVE, age_days=1),
        make_fact("plays tennis on Sundays", domain=FactDomain.PREFERENCE, type=FactType.PRINCIPLE),
        make_fact("morning yoga 30 min", domain=FactDomain.PREFERENCE, tags=["mindset"]),
        make_fact("Alex's car insurance with Example Mutual Insurance", domain=FactDomain.FINANCE),
    ]
    for fact in facts:
        await repo.add_fact(fact)

    read = await repo.get_active_facts_ordered(ACC, domain=DIRECTIVE_DOMAIN)

    assert {f.text for f in read} == {f.text for f in facts if is_directive(f)} == {
        "suggest only vegetarian recipes", "answer in English"}


@pytest.mark.parametrize("over", [
    {"domain": FactDomain.AGENT_DIRECTIVE},
    {"domain": FactDomain.PREFERENCE, "type": FactType.PRINCIPLE},
    {"domain": FactDomain.PREFERENCE, "tags": ["Mindset"]},
    {"domain": FactDomain.PREFERENCE},
    {"domain": None, "type": FactType.STATE},
])
def test_expiry_pin_parity_is_the_directive_or_a_principle_or_a_mindset_fact(over):
    """Former rule (`expiry_pinned`): directive domain, principle or the `mindset` tag. Its directive leg is the
    predicate's marker; the other two legs stay the pin's own."""
    fact = make_fact(DIRECTIVE_TEXT, **over)
    expected = (is_directive(fact) or fact.type == FactType.PRINCIPLE
                or "mindset" in {t.lower() for t in fact.tags})
    assert expiry_pinned(fact.domain, fact.type, fact.tags) is expected


def test_a_principle_and_a_mindset_fact_are_pinned_but_not_directives():
    principle = make_fact("suggest only vegetarian recipes", domain=FactDomain.PREFERENCE, type=FactType.PRINCIPLE)
    mindset = make_fact("answer in English", domain=FactDomain.PREFERENCE, tags=["mindset"])
    for fact in (principle, mindset):
        assert expiry_pinned(fact.domain, fact.type, fact.tags) is True and is_directive(fact) is False


def test_expiry_pin_reads_a_stored_string_domain_as_the_marker():
    assert expiry_pinned("agent_directive", FactType.EVENT, []) is True
    assert expiry_pinned("preference", FactType.EVENT, []) is False
