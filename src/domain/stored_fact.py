from dataclasses import dataclass

from .entities import FactEntity


@dataclass(frozen=True)
class StoredFact:
    """A fact document as read, with the store's change token. The token is compared for equality only."""

    fact: FactEntity
    update_time: object
