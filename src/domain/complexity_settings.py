from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator
from .user import PerformanceTier
from .task_complexity import TaskComplexity

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

# Resolve forward-refs here (not in user.py) to avoid a circular import:
# complexity_settings imports PerformanceTier from user.py, so user.py finishes
# loading BEFORE ComplexitySettings exists — rebuild there would fail.
# By the time this tail runs, both UserBotConfig and BillingAccount are defined.
from .user import UserBotConfig  # noqa: E402
from .billing import BillingAccount  # noqa: E402
UserBotConfig.model_rebuild()
BillingAccount.model_rebuild()
