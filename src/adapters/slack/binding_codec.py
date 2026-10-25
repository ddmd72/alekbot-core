"""The Slack codec of an `InteractiveBinding`: the one rule for turning it into a block id and back, so the poster
(`SlackInteractiveMessageAdapter`) and the interactivity route cannot disagree."""
from typing import Optional

from pydantic import ValidationError

from ...domain.interactive_binding import InteractiveBinding

# Slack's limit for a block id.
MAX_BLOCK_ID_CHARS = 255


def encode(binding: InteractiveBinding) -> str:
    """The block id that carries `binding`: `<kind>:<id>`. Raises ValueError when Slack would refuse it as too long."""
    block_id = f"{binding.kind}:{binding.id}"
    if len(block_id) > MAX_BLOCK_ID_CHARS:
        raise ValueError(f"a block id is at most {MAX_BLOCK_ID_CHARS} characters, this one is {len(block_id)}")
    return block_id


def decode(block_id: str) -> Optional[InteractiveBinding]:
    """The binding an `encode` result came from; None for anything else (a foreign or damaged block id is not an
    error). The id may itself carry a colon: only the first one separates the kind."""
    kind, separator, ident = block_id.partition(":")
    if not separator:
        return None
    try:
        return InteractiveBinding(kind=kind, id=ident)
    except ValidationError:
        return None
