"""What the owner's pick (or the question closing without one) leaves on the message that asked."""
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator

from .interactive_message import InteractiveMessage
from .interactive_option import InteractiveOption


class ChoiceOutcome(BaseModel):
    """The message to re-render: without its controls, showing the option chosen, or only `note` when none was (a timeout, a
    close elsewhere). Either an option or a note is always present.

    `keep_options` (default off) is for a pick that was refused and left the question open: the note is shown and the
    options stay for another try. It needs a note and no chosen option."""

    model_config = ConfigDict(frozen=True)

    message: InteractiveMessage
    chosen_option_id: Optional[str] = None
    note: str = ""
    keep_options: bool = False

    @model_validator(mode="after")
    def _says_something(self) -> "ChoiceOutcome":
        if self.chosen_option_id is not None and self.message.option(self.chosen_option_id) is None:
            raise ValueError("chosen option is not one of the message's options")
        if self.chosen_option_id is None and not self.note.strip():
            raise ValueError("an outcome without a chosen option needs a note")
        if self.keep_options and self.chosen_option_id is not None:
            raise ValueError("an outcome that keeps the options chooses none")
        return self

    @property
    def chosen_option(self) -> Optional[InteractiveOption]:
        return None if self.chosen_option_id is None else self.message.option(self.chosen_option_id)
