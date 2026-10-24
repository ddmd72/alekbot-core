"""What an interactive message is about: the kind routes a choice to its handler, the id names the thing asked."""
import re

from pydantic import BaseModel, ConfigDict, field_validator

_KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_MAX_ID = 128  # room for a uuid and then some; how a platform carries the binding is its adapter's codec


class InteractiveBinding(BaseModel):
    """What an interactive message is about: `kind` names the handler that answers it, `id` the thing asked. How a
    platform carries a binding in its own callback field (and reads it back) is that platform adapter's codec."""

    model_config = ConfigDict(frozen=True)

    kind: str
    id: str

    @field_validator("kind")
    @classmethod
    def _kind_is_a_name(cls, value: str) -> str:
        if not _KIND.match(value):
            raise ValueError("binding kind must be a lowercase name of letters, digits and '_'")
        return value

    @field_validator("id")
    @classmethod
    def _id_is_one_word(cls, value: str) -> str:
        if not value or len(value) > _MAX_ID or any(ch.isspace() for ch in value):
            raise ValueError(f"binding id must be 1-{_MAX_ID} characters without whitespace")
        return value
