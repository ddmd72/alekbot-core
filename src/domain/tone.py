"""User tone domain model for routing and response control."""

from enum import Enum
from .agent import RoutingMetadata
from .task_complexity import TaskComplexity


class UserTone(str, Enum):
    """Canonical user tone values produced by triage classification."""

    # Humor allowed
    CASUAL = "casual"
    FRIENDLY = "friendly"
    PLAYFUL = "playful"
    NEUTRAL = "neutral"

    # No humor
    PROFESSIONAL = "professional"
    URGENT = "urgent"
    CONCERNED = "concerned"
    DISTRESSED = "distressed"
    FORMAL = "formal"

    @classmethod
    def allows_humor(cls, tone: str) -> bool:
        """Return True if the tone allows humor/wit."""
        return tone in {cls.CASUAL, cls.FRIENDLY, cls.PLAYFUL, cls.NEUTRAL}

    @classmethod
    def validate(cls, tone) -> str:
        """Validate tone (str or UserTone) and return its canonical lowercase value.

        Falls back to "friendly" on invalid input. Accepts both raw strings
        and UserTone members; always returns the underlying string value so
        downstream comparisons (against enum.value or against equal strings)
        are stable across Python 3.10/3.11+ str-Enum repr changes.
        """
        if isinstance(tone, cls):
            return tone.value
        if isinstance(tone, str) and tone in cls._value2member_map_:
            return tone
        return cls.FRIENDLY.value


def build_routing_metadata(classification: dict) -> RoutingMetadata:
    """Build RoutingMetadata from raw triage classification JSON."""
    metadata = classification.get("metadata", {}) if classification else {}
    if not metadata:
        is_simple = classification.get("is_simple") if classification else None
        is_personal = classification.get("is_personal") if classification else None
        needs_external = classification.get("needs_external") if classification else None
        metadata = {
            "task_complexity": (
                TaskComplexity.SMALL_TALK.value if is_simple
                else TaskComplexity.SIMPLE_ANALYTICS.value
            ),
            "needs_tools": ["web_search"] if needs_external else [],
            "user_tone": UserTone.FRIENDLY,
            "reasoning": "rule_based"
        }
        if is_personal:
            metadata["needs_tools"].append("memory_search")

    task_complexity = metadata.get("task_complexity")
    if not task_complexity and "complexity_score" in metadata:
        # Transitional: tolerate legacy numeric classifications if the Firestore
        # router prompt token still emits complexity_score. Drop this branch once
        # the prompt is migrated to task_complexity.
        score = int(metadata["complexity_score"])
        if score <= 2:
            task_complexity = TaskComplexity.SMALL_TALK.value
        elif score <= 5:
            task_complexity = TaskComplexity.INFO_SEARCH.value
        elif score <= 8:
            task_complexity = TaskComplexity.SIMPLE_ANALYTICS.value
        else:
            task_complexity = TaskComplexity.DEEP_REASONING.value

    return RoutingMetadata(
        user_tone=UserTone.validate(metadata.get("user_tone", UserTone.FRIENDLY)),
        task_complexity=_safe_complexity(task_complexity),
        needs_tools=list(metadata.get("needs_tools", [])),
        reasoning=classification.get("reasoning", ""),
        semantic_lens=list(classification.get("semantic_lens", [])),
        needs_memory_search=bool(classification.get("needs_memory_search", False))
    )


def _safe_complexity(value) -> TaskComplexity:
    """Coerce router output to TaskComplexity; unknown → SIMPLE_ANALYTICS (Q4 safety net)."""
    if isinstance(value, TaskComplexity):
        return value
    try:
        return TaskComplexity(value) if value else TaskComplexity.SIMPLE_ANALYTICS
    except ValueError:
        return TaskComplexity.SIMPLE_ANALYTICS
