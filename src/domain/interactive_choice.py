"""The owner's pick on an interactive message, as a platform reports it."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .interactive_binding import InteractiveBinding
from .messaging import MessageRef


class InteractiveChoice(BaseModel):
    """Who picked which option on which message. `platform_user_id` is the platform's own id of the clicker (not yet a
    user of ours); `message_ref` is the message the control sat on. It round-trips through `model_dump(mode="json")`,
    which is how it travels in a task payload."""

    model_config = ConfigDict(frozen=True)

    binding: InteractiveBinding
    option_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    platform_user_id: str = Field(min_length=1)
    message_ref: MessageRef
    at: datetime
