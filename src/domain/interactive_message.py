"""A message that asks the owner to pick one of a few options, independent of any platform."""
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .chat_text import unwrapped
from .interactive_binding import InteractiveBinding
from .interactive_option import InteractiveOption

MIN_OPTIONS = 2
MAX_OPTIONS = 5


class InteractiveMessage(BaseModel):
    """What is asked (`prompt`), the quoted subject (`quote`, may be empty), the options in order, the binding that
    routes the answer, and `reply_hint`, the line that says how to answer without controls (by replying with a digit).

    The options are numbered from 1 in their order: a whole reply that is that digit picks the option, on every
    platform and in every language (`option_for_reply`); the digit rule lives here (`numbered_options`) and nowhere else.
    How the options, the quote and the hint are laid out is each platform adapter's business."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    options: Tuple[InteractiveOption, ...]
    binding: InteractiveBinding
    quote: str = ""
    reply_hint: str = ""

    @field_validator("prompt")
    @classmethod
    def _prompt_is_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be empty")
        return value

    @model_validator(mode="after")
    def _options_are_a_short_distinct_list(self) -> "InteractiveMessage":
        if not MIN_OPTIONS <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"an interactive message offers {MIN_OPTIONS}-{MAX_OPTIONS} options")
        ids = [option.id for option in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        return self

    def option(self, option_id: str) -> Optional[InteractiveOption]:
        return next((o for o in self.options if o.id == option_id), None)

    def numbered_options(self) -> Tuple[Tuple[int, InteractiveOption], ...]:
        """The options with their digits, 1 up, in order: the one digit rule, shared by whatever shows the digits and
        `option_for_reply`."""
        return tuple(enumerate(self.options, start=1))

    def options_line(self) -> str:
        """The numbered options on one line, `1 — label  2 — label`: how a reader without controls sees them, and the
        history text the model reads, one string for both (the digit comes from `numbered_options`)."""
        return "  ".join(f"{number} — {option.label}" for number, option in self.numbered_options())

    def option_for_reply(self, text: str) -> Optional[str]:
        """The id of the option whose digit is the whole of `text`; None otherwise.

        Around the digit a chat client's punctuation is not words: surrounding whitespace, backticks that wrap the whole
        text (`unwrapped`, the rule the chat adapters use for a copied command, any number of them) and one full stop after the digit
        (`3`, `2.`)."""
        reply = unwrapped(text)
        if reply.endswith("."):
            reply = reply[:-1].rstrip()
        for number, option in self.numbered_options():
            if reply == str(number):
                return option.id
        return None
