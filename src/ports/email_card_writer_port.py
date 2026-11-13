"""EmailCardWriterPort — write the card answer of one mail (email index v2, slice 3)."""
from abc import ABC, abstractmethod

from ..domain.email_card_reply import CardWriterReply


class EmailCardWriterPort(ABC):

    @abstractmethod
    async def write(self, account_id: str, static_block: str, card_input_text: str) -> CardWriterReply:
        """One mail's lean card answer, billed to `account_id`. `static_block` is `render_card_static_block(...)` (the
        per-account system part); `card_input_text` is `CardInput.text`.

        Never raises: every failure is a status. `invalid` and `refused` are this mail's failure; `unavailable` is an
        outage (the caller leaves the card pending and does not count it).
        """
