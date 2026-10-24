"""What an interactive message is about: the kind routes a choice to its handler, the id names the thing asked."""
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_MAX_ID = 200


class InteractiveBinding(BaseModel):
    """The one rule for turning a binding into a platform callback field and back (`token` / `parse`), so a poster and
    the inbound route cannot disagree. The longest token (241 characters) fits the tightest platform field (255)."""

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

    def token(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def parse(cls, token: str) -> Optional["InteractiveBinding"]:
        """The binding a `token()` came from; None for anything else (a foreign or damaged field is not an error)."""
        kind, separator, ident = token.partition(":")
        if not separator:
            return None
        try:
            return cls(kind=kind, id=ident)
        except ValidationError:
            return None
