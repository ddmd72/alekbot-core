"""Wiring of the email card search: the email agent searches the cards (email index v2 §5, §6.5).

The card search reads the live facts collection through the one `FirestoreEmailCardIndex` and shares the graph's one
embedding adapter and confirmed-people list. It is built unconditionally: v1 search and its switch are gone
(EMAIL_INDEX_V2_RFC slice 10).
"""
from typing import Any

from ..adapters.firestore_email_card_index import FirestoreEmailCardIndex
from ..services.email_card_search_service import EmailCardSearchService


def build_email_card_search(db_client: Any, env_config: Any, embeddings: Any,
                            people: Any) -> EmailCardSearchService:
    """The card search over the facts collection."""
    return EmailCardSearchService(FirestoreEmailCardIndex(db_client, env_config), embeddings, people)
