from typing import Any, Dict, Optional, TYPE_CHECKING
from pydantic import BaseModel, Field, field_validator
from .user import PerformanceTier
from .task_complexity import TaskComplexity

if TYPE_CHECKING:
    from .user import UserBotConfig

class ComplexitySettings(BaseModel):
    tier: PerformanceTier
    thinking_effort: Optional[str] = None
    intent_remap: Dict[str, str] = Field(default_factory=dict)
    provider_override: Optional[str] = None

    @field_validator("intent_remap", mode="before")
    @classmethod
    def _coerce_intent_remap(cls, v: Any) -> Dict[str, str]:
        # Firestore may store empty string when the field was left blank
        if not isinstance(v, dict):
            return {}
        return v

    @field_validator("thinking_effort", "provider_override", mode="before")
    @classmethod
    def _coerce_optional_str(cls, v: Any) -> Optional[str]:
        return v if v else None


DEFAULT_COMPLEXITY_SETTINGS: Dict[TaskComplexity, ComplexitySettings] = {
    TaskComplexity.SMALL_TALK: ComplexitySettings(tier=PerformanceTier.ECO),
    TaskComplexity.INFO_SEARCH: ComplexitySettings(tier=PerformanceTier.BALANCED),
    TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(
        tier=PerformanceTier.BALANCED, thinking_effort="low"
    ),
    TaskComplexity.DEEP_REASONING: ComplexitySettings(
        tier=PerformanceTier.PERFORMANCE, thinking_effort="high"
    ),
}


def resolve_complexity_settings(
    complexity: TaskComplexity,
    config: "UserBotConfig",
) -> Optional[ComplexitySettings]:
    """Merge a user's ``complexity_settings_overrides`` onto the system defaults.

    THE single implementation of this merge. It used to live inline inside
    ``TaskExecutionResolver.resolve`` while ``WorkerHandler`` read the raw defaults
    table for the same complexity — so a user who overrode ``simple_analytics`` to
    PERFORMANCE had the work run at PERFORMANCE while the SLA clock was set for
    BALANCED, and the reminder was killed at 600s instead of 1500s (2026-08-15).
    Two readings of one setting is the bug; one function is the fix.

    Per field: an override wins when it is set, otherwise the default stands — a
    partial override (e.g. tier only) keeps the default ``thinking_effort``.
    Returns ``None`` for a complexity with no default entry.
    """
    default_settings = DEFAULT_COMPLEXITY_SETTINGS.get(complexity)
    if not default_settings:
        return None

    override = config.complexity_settings_overrides.get(complexity)
    if override is None:
        return default_settings

    return ComplexitySettings(
        tier=override.tier or default_settings.tier,
        thinking_effort=(
            override.thinking_effort
            if override.thinking_effort is not None
            else default_settings.thinking_effort
        ),
        intent_remap=override.intent_remap or default_settings.intent_remap,
        provider_override=override.provider_override or default_settings.provider_override,
    )

# Resolve forward-refs here (not in user.py) to avoid a circular import:
# complexity_settings imports PerformanceTier from user.py, so user.py finishes
# loading BEFORE ComplexitySettings exists — rebuild there would fail.
# By the time this tail runs, both UserBotConfig and BillingAccount are defined.
from .user import UserBotConfig  # noqa: E402
from .billing import BillingAccount  # noqa: E402
UserBotConfig.model_rebuild()
BillingAccount.model_rebuild()
