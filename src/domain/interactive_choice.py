"""The owner's pick on an interactive message, as a platform reports it."""
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .interactive_binding import InteractiveBinding
from .messaging import MessageRef


class InteractiveChoice(BaseModel):
    """Who picked which option on which message. `platform_user_id` is the platform's own id of the clicker (not yet a
    user of ours); `message_ref` is the message the control sat on, on the same platform as the choice.

    It travels in a task payload as `to_payload()` (plain JSON types; the house encoder alone cannot write the `at`
    datetime) and comes back with `from_payload()`, which gives None for a payload that is not a choice."""

    model_config = ConfigDict(frozen=True)

    binding: InteractiveBinding
    option_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    platform_user_id: str = Field(min_length=1)
    message_ref: MessageRef
    at: datetime

    @model_validator(mode="after")
    def _ref_is_on_the_choices_platform(self) -> "InteractiveChoice":
        if self.message_ref.platform != self.platform:
            raise ValueError("the message reference must be on the choice's platform")
        return self

    def to_payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, data: Any) -> Optional["InteractiveChoice"]:
        try:
            return cls.model_validate(data)
        except ValidationError:
            return None
