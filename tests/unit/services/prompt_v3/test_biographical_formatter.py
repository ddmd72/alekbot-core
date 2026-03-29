"""
Unit tests for BiographicalFactsFormatter.

Coverage:
  format()
    - empty list → empty string
    - biographical facts → grouped under "Biographical" header
    - multiple domains → biographical first, others alphabetical
    - semantic facts → separate "Query-Specific Context" section
    - unknown domain → title-cased fallback label

  _group_by_domain()
    - biographical sorted oldest→newest
    - others sorted newest→oldest
    - semantic (semantic_lens tag) kept in insertion order
    - non-dict facts skipped
    - facts with empty text skipped

  _render_section()
    - preference + mindset tag → [MINDSET] prefix
    - preference without mindset tag → no prefix
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


def _fact(text, domain="biographical", tags=None, created_at="2026-01-01T00:00:00Z"):
    return {"text": text, "domain": domain, "tags": tags or [], "created_at": created_at}


# ---------------------------------------------------------------------------
# format()
# ---------------------------------------------------------------------------

class TestFormat:

    def test_empty_list_returns_empty_string(self, fmt):
        assert fmt.format([]) == ""

    def test_biographical_facts_grouped_under_biographical_header(self, fmt):
        facts = [_fact("Born in Spain", domain="biographical")]
        result = fmt.format(facts)
        assert "**Biographical**" in result
        assert "Born in Spain" in result

    def test_biographical_section_comes_first(self, fmt):
        facts = [
            _fact("Work fact", domain="work"),
            _fact("Bio fact", domain="biographical"),
        ]
        result = fmt.format(facts)
        bio_idx = result.index("**Biographical**")
        work_idx = result.index("**Work**")
        assert bio_idx < work_idx

    def test_non_biographical_domains_sorted_alphabetically(self, fmt):
        facts = [
            _fact("Work fact", domain="work"),
            _fact("Health fact", domain="health"),
        ]
        result = fmt.format(facts)
        health_idx = result.index("**Health**")
        work_idx = result.index("**Work**")
        assert health_idx < work_idx

    def test_semantic_facts_in_separate_section(self, fmt):
        facts = [
            _fact("Regular bio", domain="biographical"),
            _fact("Context hint", domain="biographical", tags=["semantic_lens"]),
        ]
        result = fmt.format(facts)
        assert "**Query-Specific Context:**" in result
        assert "Context hint" in result

    def test_unknown_domain_gets_titlecased_label(self, fmt):
        facts = [_fact("Custom fact", domain="custom_domain")]
        result = fmt.format(facts)
        assert "**Custom Domain**" in result

    def test_known_domain_uses_predefined_label(self, fmt):
        facts = [_fact("Fact", domain="medical_records")]
        result = fmt.format(facts)
        assert "**Medical Records**" in result

    def test_sections_separated_by_double_newline(self, fmt):
        facts = [
            _fact("Bio fact", domain="biographical"),
            _fact("Work fact", domain="work"),
        ]
        result = fmt.format(facts)
        assert "\n\n" in result


# ---------------------------------------------------------------------------
# _group_by_domain()
# ---------------------------------------------------------------------------

class TestGroupByDomain:

    def test_biographical_sorted_oldest_to_newest(self, fmt):
        facts = [
            _fact("Newer", domain="biographical", created_at="2026-03-01T00:00:00Z"),
            _fact("Older", domain="biographical", created_at="2025-01-01T00:00:00Z"),
        ]
        grouped = fmt._group_by_domain(facts)
        assert grouped["biographical"][0]["text"] == "Older"
        assert grouped["biographical"][1]["text"] == "Newer"

    def test_non_biographical_sorted_newest_to_oldest(self, fmt):
        facts = [
            _fact("Older", domain="work", created_at="2025-01-01T00:00:00Z"),
            _fact("Newer", domain="work", created_at="2026-03-01T00:00:00Z"),
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
        assert fmt._render_section("Biographical", [], "biographical") == ""

    def test_mindset_prefix_for_preference_domain_with_mindset_tag(self, fmt):
        facts = [_fact("Be curious", domain="preference", tags=["mindset"])]
        result = fmt._render_section("Preference", facts, "preference")
        assert "[MINDSET] Be curious" in result

    def test_no_mindset_prefix_without_mindset_tag(self, fmt):
        facts = [_fact("Likes coffee", domain="preference", tags=[])]
        result = fmt._render_section("Preference", facts, "preference")
        assert "[MINDSET]" not in result
        assert "Likes coffee" in result

    def test_no_mindset_prefix_for_non_preference_domain(self, fmt):
        facts = [_fact("Runs daily", domain="health", tags=["mindset"])]
        result = fmt._render_section("Health", facts, "health")
        assert "[MINDSET]" not in result

    def test_date_suffix_appended(self, fmt):
        facts = [_fact("Born in Spain", created_at="2026-01-15T00:00:00Z")]
        result = fmt._render_section("Biographical", facts, "biographical")
        assert "Jan 15, 2026" in result

    def test_no_date_suffix_when_created_at_missing(self, fmt):
        facts = [{"text": "Old fact", "domain": "biographical", "tags": []}]
        result = fmt._render_section("Biographical", facts, "biographical")
        assert "(" not in result

    def test_header_and_bullet_format(self, fmt):
        facts = [_fact("A fact", created_at=None)]
        result = fmt._render_section("Biographical", facts, "biographical")
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
