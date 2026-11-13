from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping, Optional, Sequence

from ..domain.email_card_lifecycle import CardExpect, CardWriteResult


class EmailCardStore(ABC):
    """Where a stored mail's v2 card lives: extra fields on the mail's v1 document (email index v2 §4, §6.3).

    Holds for every method: tenancy is the user id (a document of another user is never read or written); the card
    path never creates a document and never touches a v1 field; backend errors propagate.
    """

    @abstractmethod
    async def read_expect(self, user_id: str, email_ids: Sequence[str]) -> Dict[str, CardExpect]:
        """The CAS pair of each existing stored mail (absent ones left out), by projection; at most 500 ids per call."""

    @abstractmethod
    async def write_card(self, user_id: str, email_id: str, fields: Mapping[str, Any],
                         vector: Optional[Sequence[float]], expect: CardExpect) -> CardWriteResult:
        """In one transaction: read the stored card_version and people_digest; MISSING when the document does not
        exist (never created here); CONFLICT when they differ from `expect`; else update `fields` (+ `card_vector`
        when a vector is given; `card_vector` deleted when None) and WRITTEN. Only CARD_FIELDS are written; any other
        key (or `card_vector` among `fields`) -> ValueError before any I/O. Never touches a v1 field."""
