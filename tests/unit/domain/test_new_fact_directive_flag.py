"""FACT_PROFILE_RFC §9 row 13: a new fact's `directive` decision as `parse_new_fact` reads it, and what a search hit
shows the consolidator of a stored fact (`consolidation_hit`: the profile's `primary_domain` and the flag, no old domain).
"""
from datetime import datetime, timezone

import pytest

from src.domain.entities import FactEntity, FactType
from src.domain.new_fact_attributes import new_fact_decision, parse_new_fact
from src.domain.search import EnrichedFact, consolidation_hit

TEXT = "Suggest only vegetarian recipes."


@pytest.mark.parametrize("raw, expected", [(True, True), (False, False)])
def test_a_boolean_directive_is_taken_as_sent(raw, expected):
    assert parse_new_fact({"content": TEXT, "directive": raw}).directive is expected


def test_a_missing_or_null_directive_reads_as_no_directive():
    assert parse_new_fact({"content": TEXT}).directive is False
    assert parse_new_fact({"content": TEXT, "directive": None}).directive is False


@pytest.mark.parametrize("raw", ["true", "false", 1, 0, "AGENT_DIRECTIVE", ["yes"]])
def test_a_directive_that_is_not_a_boolean_is_refused(raw):
    with pytest.raises(ValueError, match="directive"):
        parse_new_fact({"content": TEXT, "directive": raw})


def test_an_old_domain_key_is_not_read():
    attrs = parse_new_fact({"content": TEXT, "domain": "agent_directive"})
    assert attrs.directive is False and not hasattr(attrs, "domain")


def test_a_directive_is_pinned_and_needs_no_expiry():
    directive = parse_new_fact({"content": TEXT, "directive": True})
    plain = parse_new_fact({"content": "Alex plays tennis on Sundays.", "directive": False})
    assert new_fact_decision(directive, "create_fact") is not None   # pinned: `none` decided for it
    with pytest.raises(ValueError, match="expiry"):
        new_fact_decision(plain, "create_fact")


def _hit(**over):
    fact = FactEntity(id="f1", account_id="a", created_by_user_id="u", lineage_id="f1", text=TEXT, type=FactType.EVENT,
                      reported_date=datetime(2026, 10, 20, 9, tzinfo=timezone.utc), **over)
    return consolidation_hit(EnrichedFact.from_fact(fact, "keyword", 0.8), "Europe/Berlin")


def test_a_search_hit_shows_the_profile_domain_and_the_flag_and_no_old_domain():
    hit = _hit(directive=True, primary_domain="agent_directives")
    assert (hit["primary_domain"], hit["directive"]) == ("agent_directives", True)
    assert "domain" not in hit


def test_an_unprofiled_plain_fact_shows_no_domain_and_no_directive():
    hit = _hit()
    assert (hit["primary_domain"], hit["directive"]) == (None, False)
