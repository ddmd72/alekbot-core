"""One choice offered in an interactive message."""
import re

from pydantic import BaseModel, ConfigDict, field_validator

_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# The tightest button text of the platforms we post to; a longer label would make the whole post fail there.
# The question-label check of the memory review (T1d `check_labels`) bounds the option labels by this constant too, so
# a label set that passes it can always be posted.
MAX_LABEL_CHARS = 75


class InteractiveOption(BaseModel):
    """`id` is what comes back when the owner picks it; `label` is what he reads (one line, any language)."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str

    @field_validator("id")
    @classmethod
    def _id_is_token_safe(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("option id must be 1-64 characters of letters, digits, '_', '.', '-'")
        return value

    @field_validator("label")
    @classmethod
    def _label_is_one_line(cls, value: str) -> str:
        label = value.strip()
        if not label or "\n" in label or "\r" in label or len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"option label must be a non-empty single line of at most {MAX_LABEL_CHARS} characters")
        return label
