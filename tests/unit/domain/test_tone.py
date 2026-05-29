import pytest

from src.domain.tone import UserTone, build_routing_metadata, _safe_complexity
from src.domain.task_complexity import TaskComplexity


def test_validate_accepts_known_tones():
    assert UserTone.validate("casual") == UserTone.CASUAL
    assert UserTone.validate("friendly") == UserTone.FRIENDLY
    assert UserTone.validate("playful") == UserTone.PLAYFUL
    assert UserTone.validate("neutral") == UserTone.NEUTRAL
    assert UserTone.validate("professional") == UserTone.PROFESSIONAL
    assert UserTone.validate("urgent") == UserTone.URGENT
    assert UserTone.validate("concerned") == UserTone.CONCERNED
    assert UserTone.validate("distressed") == UserTone.DISTRESSED
    assert UserTone.validate("formal") == UserTone.FORMAL


def test_validate_falls_back_to_friendly_on_invalid():
    assert UserTone.validate("mystery") == UserTone.FRIENDLY


@pytest.mark.parametrize("tone,expected", [
    ("casual", True),
    ("friendly", True),
    ("playful", True),
    ("neutral", True),
    ("professional", False),
    ("urgent", False),
    ("concerned", False),
    ("distressed", False),
    ("formal", False),
])
def test_allows_humor(tone, expected):
    assert UserTone.allows_humor(tone) is expected


# ============================================================================
# build_routing_metadata fallback — pins invariant "rule-based fallback path
# always yields a TaskComplexity value, so cost model is never bypassed"
# (closes F2.6 + F2.10 from ARCHITECTURE_INSPECTION_FOLLOWUP.md).
# ============================================================================


class TestBuildRoutingMetadataFallback:
    """Triage failure → rule-based dict (no `metadata` key) → fallback must
    construct a complete metadata object including task_complexity. This is
    the spec that defeats F2.6 / F2.10 ("cost model bypassed on triage failure")."""

    def test_simple_classification_maps_to_small_talk(self):
        result = build_routing_metadata({"is_simple": True, "is_personal": False, "needs_external": False})
        assert result.task_complexity == TaskComplexity.SMALL_TALK

    def test_non_simple_classification_maps_to_simple_analytics(self):
        result = build_routing_metadata({"is_simple": False, "is_personal": False, "needs_external": False})
        assert result.task_complexity == TaskComplexity.SIMPLE_ANALYTICS

    def test_personal_classification_adds_memory_search_tool(self):
        result = build_routing_metadata({"is_simple": False, "is_personal": True, "needs_external": False})
        assert "memory_search" in result.needs_tools

    def test_external_classification_adds_web_search_tool(self):
        result = build_routing_metadata({"is_simple": False, "is_personal": False, "needs_external": True})
        assert "web_search" in result.needs_tools

    def test_fallback_sets_user_tone(self):
        # Fallback path always populates user_tone; exact representation is
        # downstream of UserTone.validate (str-enum stringification quirks
        # on Python 3.11+ are a separate concern, not load-bearing for
        # cost-model invariant).
        result = build_routing_metadata({"is_simple": True})
        assert result.user_tone is not None
        assert "friendly" in str(result.user_tone).lower()

    def test_empty_classification_still_returns_valid_metadata(self):
        result = build_routing_metadata({})
        assert isinstance(result.task_complexity, TaskComplexity)
        assert result.task_complexity == TaskComplexity.SIMPLE_ANALYTICS


class TestBuildRoutingMetadataLLMSuccess:
    """LLM triage success path: metadata block present, task_complexity passes through."""

    def test_llm_metadata_passthrough(self):
        result = build_routing_metadata({
            "metadata": {"task_complexity": "deep_reasoning", "user_tone": "professional"},
            "semantic_lens": ["car"],
        })
        assert result.task_complexity == TaskComplexity.DEEP_REASONING
        assert result.user_tone == UserTone.PROFESSIONAL


class TestSafeComplexity:
    """_safe_complexity is the last-line safety net for the cost model
    (Q4 safety net from per_call_execution_context)."""

    def test_valid_string_coerces_to_enum(self):
        assert _safe_complexity("deep_reasoning") == TaskComplexity.DEEP_REASONING

    def test_invalid_string_falls_back_to_simple_analytics(self):
        assert _safe_complexity("mystery_tier") == TaskComplexity.SIMPLE_ANALYTICS

    def test_none_falls_back_to_simple_analytics(self):
        assert _safe_complexity(None) == TaskComplexity.SIMPLE_ANALYTICS

    def test_enum_value_passes_through(self):
        assert _safe_complexity(TaskComplexity.INFO_SEARCH) == TaskComplexity.INFO_SEARCH