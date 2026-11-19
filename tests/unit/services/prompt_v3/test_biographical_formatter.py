"""
Unit tests for BiographicalFactsFormatter.

Coverage:
  format()  (sections by the profile's primary_domain, FACT_PROFILE_RFC §6 row 3)
    - empty list → empty string
    - biography facts → grouped under the registry label "Biography"
    - multiple domains → biography first, others in registry order
    - semantic facts → separate "Query-Specific Context" section
    - unknown primary_domain → the Lifestyle section

  _group_by_domain()
    - biography sorted oldest→newest
    - others sorted newest→oldest
    - semantic (semantic_lens tag) kept in insertion order
    - non-dict facts skipped
    - facts with empty text skipped

  _render_section()
    - mindset tag → [MINDSET] prefix, in any domain
    - no mindset tag → no prefix
    - date suffix appended when created_at present
    - empty facts list → empty string

  _render_semantic_section()
    - facts rendered as bullet list under "Query-Specific Context:"
    - empty list → empty string

  _format_date()
    - datetime object → "Mon DD, YYYY"
    - ISO string with Z → parsed correctly
    - ISO string with +00:00 → parsed correctly
    - None → empty string
    - invalid string → empty string
"""
from datetime import datetime, timezone

import pytest

from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter


@pytest.fixture
def fmt():
    return BiographicalFactsFormatter()


def _fact(text, domain="biographical", tags=None, created_at="2026-01-01T00:00:00Z", primary=None):
    fact = {"text": text, "domain": domain, "tags": tags or [], "created_at": created_at}
    if primary is not None:
        fact["primary_domain"] = primary
    return fact


# ---------------------------------------------------------------------------
# format()
# ---------------------------------------------------------------------------

class TestFormat:

    def test_empty_list_returns_empty_string(self, fmt):
        assert fmt.format([]) == ""

    def test_biographical_facts_grouped_under_biographical_header(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): the section is the registry label of primary_domain.
        facts = [_fact("Born in Portugal", primary="biography")]
        result = fmt.format(facts)
        assert "**Biography**" in result
        assert "Born in Portugal" in result

    def test_biographical_section_comes_first(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): sections keyed on primary_domain, biography first.
        facts = [
            _fact("Work fact", primary="work"),
            _fact("Bio fact", primary="biography"),
        ]
        result = fmt.format(facts)
        bio_idx = result.index("**Biography**")
        work_idx = result.index("**Work**")
        assert bio_idx < work_idx

    def test_non_biographical_domains_follow_the_registry_order(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): alphabetical order is replaced by registry order; sport
        # precedes food in the registry and follows it alphabetically, so the pair tells the two orders apart.
        facts = [
            _fact("Food fact", primary="food"),
            _fact("Sport fact", primary="sport"),
        ]
        result = fmt.format(facts)
        sport_idx = result.index("**Sport**")
        food_idx = result.index("**Food & drinks**")
        assert sport_idx < food_idx

    def test_semantic_facts_in_separate_section(self, fmt):
        facts = [
            _fact("Regular bio", domain="biographical"),
            _fact("Context hint", domain="biographical", tags=["semantic_lens"]),
        ]
        result = fmt.format(facts)
        assert "**Query-Specific Context:**" in result
        assert "Context hint" in result

    def test_unknown_domain_lands_in_the_lifestyle_section(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): an unknown primary_domain gets no title-cased section of
        # its own; it lands in the Lifestyle section.
        facts = [_fact("Custom fact", primary="custom_domain")]
        result = fmt.format(facts)
        assert result.startswith("**Lifestyle**\n- Custom fact")
        assert "**Custom Domain**" not in result and "custom_domain" not in result

    def test_known_domain_uses_predefined_label(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): the label is the registry's English label of primary_domain;
        # the old domain value names no section.
        facts = [_fact("Fact", domain="medical_records", primary="health")]
        result = fmt.format(facts)
        assert "**Health & wellbeing**" in result
        assert "**Medical Records**" not in result

    def test_sections_separated_by_double_newline(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): two sections come from two primary domains.
        facts = [
            _fact("Bio fact", primary="biography"),
            _fact("Work fact", primary="work"),
        ]
        result = fmt.format(facts)
        assert "\n\n" in result


# ---------------------------------------------------------------------------
# _group_by_domain()
# ---------------------------------------------------------------------------

class TestGroupByDomain:

    def test_biographical_sorted_oldest_to_newest(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): grouped by the registry key of primary_domain.
        facts = [
            _fact("Newer", primary="biography", created_at="2026-03-01T00:00:00Z"),
            _fact("Older", primary="biography", created_at="2025-01-01T00:00:00Z"),
        ]
        grouped = fmt._group_by_domain(facts)
        assert grouped["biography"][0]["text"] == "Older"
        assert grouped["biography"][1]["text"] == "Newer"

    def test_non_biographical_sorted_newest_to_oldest(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): grouped by the registry key of primary_domain.
        facts = [
            _fact("Older", primary="work", created_at="2025-01-01T00:00:00Z"),
            _fact("Newer", primary="work", created_at="2026-03-01T00:00:00Z"),
        ]
        grouped = fmt._group_by_domain(facts)
        assert grouped["work"][0]["text"] == "Newer"
        assert grouped["work"][1]["text"] == "Older"

    def test_semantic_lens_tag_goes_to_semantic_domain(self, fmt):
        facts = [_fact("Hint", domain="biographical", tags=["semantic_lens"])]
        grouped = fmt._group_by_domain(facts)
        assert "semantic" in grouped
        assert "biographical" not in grouped

    def test_non_dict_fact_skipped(self, fmt):
        grouped = fmt._group_by_domain(["not a dict", None, 42])
        assert grouped == {}

    def test_fact_with_empty_text_skipped(self, fmt):
        facts = [{"text": "", "domain": "biographical", "tags": [], "created_at": ""}]
        grouped = fmt._group_by_domain(facts)
        assert grouped == {}

    def test_fact_with_whitespace_only_text_skipped(self, fmt):
        facts = [{"text": "   ", "domain": "biographical", "tags": [], "created_at": ""}]
        grouped = fmt._group_by_domain(facts)
        assert grouped == {}


# ---------------------------------------------------------------------------
# _render_section()
# ---------------------------------------------------------------------------

class TestRenderSection:

    def test_empty_facts_returns_empty_string(self, fmt):
        assert fmt._render_section("Biographical", []) == ""

    def test_mindset_prefix_for_preference_domain_with_mindset_tag(self, fmt):
        facts = [_fact("Be curious", domain="preference", tags=["mindset"])]
        result = fmt._render_section("Preference", facts)
        assert "[MINDSET] Be curious" in result

    def test_no_mindset_prefix_without_mindset_tag(self, fmt):
        facts = [_fact("Likes coffee", domain="preference", tags=[])]
        result = fmt._render_section("Preference", facts)
        assert "[MINDSET]" not in result
        assert "Likes coffee" in result

    def test_mindset_prefix_for_non_preference_domain(self, fmt):
        # Reviewer edit (FACT_PROFILE_RFC §6 row 3, PR 11): [MINDSET] keys on the tag alone, in any domain.
        facts = [_fact("Runs daily", domain="health", tags=["mindset"], primary="health")]
        result = fmt._render_section("Health & wellbeing", facts)
        assert "- [MINDSET] Runs daily" in result

    def test_date_suffix_appended(self, fmt):
        facts = [_fact("Born in Portugal", created_at="2026-01-15T00:00:00Z")]
        result = fmt._render_section("Biographical", facts)
        assert "Jan 15, 2026" in result

    def test_no_date_suffix_when_created_at_missing(self, fmt):
        facts = [{"text": "Old fact", "domain": "biographical", "tags": []}]
        result = fmt._render_section("Biographical", facts)
        assert "(" not in result

    def test_header_and_bullet_format(self, fmt):
        facts = [_fact("A fact", created_at=None)]
        result = fmt._render_section("Biographical", facts)
        assert result.startswith("**Biographical**")
        assert "- A fact" in result


# ---------------------------------------------------------------------------
# _render_semantic_section()
# ---------------------------------------------------------------------------

class TestRenderSemanticSection:

    def test_empty_list_returns_empty_string(self, fmt):
        assert fmt._render_semantic_section([]) == ""

    def test_header_and_bullets(self, fmt):
        facts = [_fact("Context A"), _fact("Context B")]
        result = fmt._render_semantic_section(facts)
        assert "**Query-Specific Context:**" in result
        assert "- Context A" in result
        assert "- Context B" in result

    def test_fact_with_empty_text_skipped(self, fmt):
        facts = [{"text": "", "tags": []}]
        result = fmt._render_semantic_section(facts)
        assert result == ""


# ---------------------------------------------------------------------------
# _format_date()
# ---------------------------------------------------------------------------

class TestFormatDate:

    def test_none_returns_empty_string(self):
        assert BiographicalFactsFormatter._format_date(None) == ""

    def test_datetime_object_formatted(self):
        dt = datetime(2026, 1, 15, tzinfo=timezone.utc)
        result = BiographicalFactsFormatter._format_date(dt)
        assert result == "Jan 15, 2026"

    def test_iso_string_with_z(self):
        result = BiographicalFactsFormatter._format_date("2026-03-29T10:00:00Z")
        assert result == "Mar 29, 2026"

    def test_iso_string_with_offset(self):
        result = BiographicalFactsFormatter._format_date("2026-06-01T00:00:00+00:00")
        assert result == "Jun 01, 2026"

    def test_invalid_string_returns_empty(self):
        assert BiographicalFactsFormatter._format_date("not-a-date") == ""

    def test_unknown_type_returns_empty(self):
        assert BiographicalFactsFormatter._format_date(12345) == ""


# ---------------------------------------------------------------------------
# format_directives() — standing_directives rulebook (STANDING_DIRECTIVES_RFC)
# ---------------------------------------------------------------------------

class TestFormatDirectives:

    def test_empty_returns_empty_string(self):
        assert BiographicalFactsFormatter().format_directives([]) == ""

    def test_renders_bullets_no_dates(self):
        out = BiographicalFactsFormatter().format_directives([
            {"text": "Never give a partial answer.", "created_at": "2026-01-01T00:00:00Z"},
        ])
        assert out == "- Never give a partial answer."
        assert "2026" not in out  # directives are timeless — no date suffix

    def test_newest_first_ordering(self):
        out = BiographicalFactsFormatter().format_directives([
            {"text": "older", "created_at": "2026-01-01T00:00:00Z"},
            {"text": "newer", "created_at": "2026-06-01T00:00:00Z"},
        ])
        assert out.index("newer") < out.index("older")

    def test_skips_blank_and_non_dict(self):
        out = BiographicalFactsFormatter().format_directives([
            {"text": "  "}, "not-a-dict", {"text": "Keep this."},
        ])
        assert out == "- Keep this."
