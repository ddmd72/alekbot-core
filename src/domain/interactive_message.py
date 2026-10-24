"""A message that asks the owner to pick one of a few options, independent of any platform."""
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .interactive_binding import InteractiveBinding
from .interactive_option import InteractiveOption

MIN_OPTIONS = 2
MAX_OPTIONS = 5


class InteractiveMessage(BaseModel):
    """What is asked (`prompt`), the quoted subject (`quote`, may be empty), the options in order, the binding that
    routes the answer, and `reply_hint`, the line that says how to answer without controls (by replying with a digit):
    an adapter with controls shows it as the fallback for them, the plain text ends with it.

    The options are numbered from 1 in their order: a whole reply that is that digit picks the option, on every
    platform and in every language (`option_for_reply`); the digit rule lives here and nowhere else."""

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

    def option_for_reply(self, text: str) -> Optional[str]:
        """The id of the option whose digit is the whole of `text` (surrounding whitespace aside); None otherwise."""
        reply = text.strip()
        for number, option in enumerate(self.options, start=1):
            if reply == str(number):
                return option.id
        return None

    def as_plain_text(self) -> str:
        """Prompt, the quote in «», the numbered options on one line and last the reply hint: what a platform without
        controls shows. The hint is part of the text because there only a reply to the message counts."""
        parts = [self.prompt.strip()]
        if self.quote.strip():
            parts.append(f"«{self.quote.strip()}»")
        parts.append("  ".join(f"{number} — {option.label}" for number, option in enumerate(self.options, start=1)))
        if self.reply_hint.strip():
            parts.append(self.reply_hint.strip())
        return "\n\n".join(parts)
