"""User tone domain model for routing and response control."""

from enum import Enum
from .agent import RoutingMetadata


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
    def validate(cls, tone: str) -> str:
        """Validate tone string and fall back to friendly on invalid input."""
        if tone in cls._value2member_map_:
            return str(tone)
        return str(cls.FRIENDLY.value)


def build_routing_metadata(classification: dict) -> RoutingMetadata:
    """Build RoutingMetadata from raw triage classification JSON."""
    metadata = classification.get("metadata", {}) if classification else {}
    confidence = float(classification.get("confidence", 0.5)) if classification else 0.5
    if not metadata:
        is_simple = classification.get("is_simple") if classification else None
        is_personal = classification.get("is_personal") if classification else None
        needs_external = classification.get("needs_external") if classification else None
        metadata = {
            "complexity_score": 2 if is_simple else 6,
            "needs_tools": ["web_search"] if needs_external else [],
            "user_tone": UserTone.FRIENDLY,
            "reasoning": "rule_based"
        }
        if is_personal:
            metadata["needs_tools"].append("memory_search")
        confidence = 0.9 if is_simple else 0.8
    return RoutingMetadata(
        user_tone=UserTone.validate(metadata.get("user_tone", UserTone.FRIENDLY)),
        complexity_score=int(metadata.get("complexity_score", 5)),
        confidence=confidence,
        needs_tools=list(metadata.get("needs_tools", [])),
        reasoning=classification.get("reasoning", ""),
        semantic_lens=list(classification.get("semantic_lens", [])),
        needs_memory_search=bool(classification.get("needs_memory_search", False))
    )
