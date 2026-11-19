"""Baseline sections by the fact profile (FACT_PROFILE_RFC §6 row 3, §9 PR 11): `BiographicalFactsFormatter` heads each
section with the registry's English label of the items' `primary_domain`, biography first and the rest in registry order;
`[MINDSET]` comes from the tag on any domain; an item without a registry domain lands in the lifestyle section."""
import re

import pytest

from src.domain.fact_domains import Domain, all_keys, label
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter

BORN = "Alex Doe was born on 1980-01-01 in Springfield"
CITIZEN = "Alex Doe is a Portuguese citizen"
PARTNER = "Alex's partner is Clara Moreira"
YOGA = "Alex does morning yoga for 30 minutes"
VEGETARIAN = "Alex is vegetarian"
DIABETES = "Alex was diagnosed with type 2 diabetes in 2022"
UNION = "Alex joined the Acme employees' trade union in November 2026, dues 12 EUR a month"
TENNIS = "Alex plays tennis on Sundays"


def _item(text, primary=None, tags=None, created_at="2026-11-01T00:00:00Z", **extra):
    item = {"text": text, "tags": tags or [], "created_at": created_at, **extra}
    if primary is not None:
        item["primary_domain"] = primary
    return item


def _headers(rendered):
    return re.findall(r"^\*\*(.+?)\*\*$", rendered, flags=re.MULTILINE)


def _section(rendered, header):
    """The bullet texts (date suffix dropped) under one section header."""
    block = rendered.split(f"**{header}**\n", 1)[1].split("\n\n", 1)[0]
    return [re.sub(r" \([A-Z][a-z]{2} \d{2}, \d{4}\)$", "", line[2:]) for line in block.splitlines()]


@pytest.fixture
def fmt():
    return BiographicalFactsFormatter()


class TestSectionLabels:

    def test_section_headers_are_the_registry_english_labels(self, fmt):
        rendered = fmt.format([_item(UNION, Domain.WORK), _item(DIABETES, Domain.HEALTH), _item(BORN, Domain.BIOGRAPHY)])
        assert set(_headers(rendered)) == {label(Domain.WORK, "en"), label(Domain.HEALTH, "en"),
                                           label(Domain.BIOGRAPHY, "en")}

    def test_biography_comes_first_then_registry_order(self, fmt):
        items = [_item(UNION, Domain.WORK), _item(TENNIS, Domain.SPORT), _item(VEGETARIAN, Domain.FOOD),
                 _item(DIABETES, Domain.HEALTH), _item(PARTNER, Domain.FAMILY), _item(BORN, Domain.BIOGRAPHY)]
        present = {Domain.WORK, Domain.SPORT, Domain.FOOD, Domain.HEALTH, Domain.FAMILY, Domain.BIOGRAPHY}
        expected = [label(key, "en") for key in all_keys() if key in present]
        headers = _headers(fmt.format(items))
        assert headers[0] == label(Domain.BIOGRAPHY, "en")
        assert headers == expected

    def test_the_old_domain_field_does_not_choose_the_section(self, fmt):
        rendered = fmt.format([_item(YOGA, Domain.SPORT, domain="biographical")])
        assert _headers(rendered) == [label(Domain.SPORT, "en")]

    def test_items_of_one_domain_share_one_section(self, fmt):
        rendered = fmt.format([_item(TENNIS, Domain.SPORT), _item(YOGA, Domain.SPORT)])
        assert _headers(rendered) == [label(Domain.SPORT, "en")]
        assert set(_section(rendered, label(Domain.SPORT, "en"))) == {TENNIS, YOGA}

    def test_biography_is_oldest_first_and_other_sections_newest_first(self, fmt):
        rendered = fmt.format([
            _item(CITIZEN, Domain.BIOGRAPHY, created_at="2026-03-01T00:00:00Z"),
            _item(BORN, Domain.BIOGRAPHY, created_at="2025-01-01T00:00:00Z"),
            _item(YOGA, Domain.SPORT, created_at="2025-01-01T00:00:00Z"),
            _item(TENNIS, Domain.SPORT, created_at="2026-03-01T00:00:00Z"),
        ])
        assert _section(rendered, label(Domain.BIOGRAPHY, "en")) == [BORN, CITIZEN]
        assert _section(rendered, label(Domain.SPORT, "en")) == [TENNIS, YOGA]


class TestMindset:

    @pytest.mark.parametrize("primary", [Domain.FOOD, Domain.HEALTH, Domain.BIOGRAPHY, None])
    def test_the_mindset_tag_prefixes_the_line_in_any_domain(self, fmt, primary):
        rendered = fmt.format([_item(VEGETARIAN, primary, tags=["mindset"])])
        assert f"- [MINDSET] {VEGETARIAN}" in rendered

    def test_no_prefix_without_the_tag(self, fmt):
        rendered = fmt.format([_item(VEGETARIAN, Domain.FOOD, tags=["food"])])
        assert "[MINDSET]" not in rendered and f"- {VEGETARIAN}" in rendered


class TestNoDomain:

    @pytest.mark.parametrize("item", [
        _item(YOGA),
        _item(YOGA, primary_domain=None),
        _item(YOGA, primary_domain=""),
    ], ids=["absent", "none", "empty"])
    def test_an_item_without_a_primary_domain_lands_in_the_lifestyle_section(self, fmt, item):
        rendered = fmt.format([item, _item(BORN, Domain.BIOGRAPHY)])
        assert _headers(rendered) == [label(Domain.BIOGRAPHY, "en"), label(Domain.LIFESTYLE, "en")]
        assert _section(rendered, label(Domain.LIFESTYLE, "en")) == [YOGA]

    def test_an_unprofiled_item_joins_the_profiled_lifestyle_items(self, fmt):
        rendered = fmt.format([_item(YOGA), _item(TENNIS, Domain.LIFESTYLE)])
        assert _headers(rendered) == [label(Domain.LIFESTYLE, "en")]
        assert set(_section(rendered, label(Domain.LIFESTYLE, "en"))) == {YOGA, TENNIS}


class TestQuerySpecificContext:

    def test_prefetched_items_stay_in_their_own_section_after_the_domain_sections(self, fmt):
        rendered = fmt.format([_item(TENNIS, Domain.SPORT, tags=["semantic_lens"]), _item(BORN, Domain.BIOGRAPHY)])
        assert _headers(rendered) == [label(Domain.BIOGRAPHY, "en"), "Query-Specific Context:"]
        assert rendered.endswith(f"- {TENNIS}")
