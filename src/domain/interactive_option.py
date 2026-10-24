"""One choice offered in an interactive message."""
import re

from pydantic import BaseModel, ConfigDict, field_validator

_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


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
        if not label or "\n" in label or "\r" in label:
            raise ValueError("option label must be a non-empty single line")
        return label
