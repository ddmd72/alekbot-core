"""
EmailCardIndex — the read port of email search v2: the carded mail of one user, by key, by month and by meaning
(email index v2 §4, §5; search slice 4).

A port is justified here: two implementations (the in-memory one the tests and the snapshot harness run on, and the
Firestore one, slice 8) and a system boundary. Slice 6 deploys the Firestore indexes for exactly the query shapes
below.

Contract, every read:
- `user_id ==` and `card_state == "carded"`: v2 search reads carded cards only (an `empty`, `abandoned` or
  `source_gone` card is hidden, never deleted); tenancy is by `user_id`, as the v1 store
  (`indexed_email_repository`): another user's card is never returned;
- an invalid argument raises ValueError before any I/O; a backend error propagates.
"""
from abc import ABC, abstractmethod
from typing import List, Sequence, Tuple

from ..domain.email_card import EmailCard

KEY_FIELDS = ("about_keys", "name_keys", "people_keys")


class EmailCardIndex(ABC):
    """The carded mail of a user (module docstring). CONCEPT_KEYS_MAX (30, `drive_index_params`) bounds every key list."""

    @abstractmethod
    async def find_by_keys(self, user_id: str, field: str, keys: Sequence[str], limit: int) -> List[EmailCard]:
        """At most `limit` cards whose `field` (one of KEY_FIELDS, else ValueError) holds any of `keys`
        (`array_contains_any`; more than CONCEPT_KEYS_MAX keys raises ValueError). No order is guaranteed."""

    @abstractmethod
    async def list_in_months(self, user_id: str, months: Sequence[str], limit: int) -> List[EmailCard]:
        """At most `limit` cards whose `month_keys` hold any of `months` (`m:YYYY-MM`, at most CONCEPT_KEYS_MAX)."""

    @abstractmethod
    async def find_nearest(self, user_id: str, vector: Sequence[float], limit: int) -> List[Tuple[EmailCard, float]]:
        """At most `limit` cards nearest to `vector` by cosine distance on the card vector, nearest first, as
        `(card, distance)`. A card with no vector is not returned."""

    @abstractmethod
    async def count_carded(self, user_id: str) -> int:
        """How many cards of the user are `carded`."""

    @abstractmethod
    async def count_uncarded(self, user_id: str) -> int:
        """How many stored mails of the user have no settled card: no card fields yet, or `pending`."""

