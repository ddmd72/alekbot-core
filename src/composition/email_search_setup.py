"""Wiring of email search v2: the email agent searches the cards (email index v2 §5, §6.5; slice 8).

Behind the optional knob `EMAIL_SEARCH_V2` (read here with `os.getenv`, never in `load_settings()`): **code default
off**; `1` switches it on. Off, the email agent keeps the v1 search unchanged: the switch is the only fallback, a v2
error fails the search. The card search reads the live facts collection through the one `FirestoreEmailCardIndex` and
shares the graph's one embedding adapter and confirmed-people list.
"""
import os
from typing import Any, Optional

from ..adapters.firestore_email_card_index import FirestoreEmailCardIndex
from ..services.email_card_search_service import EmailCardSearchService
from ..utils.logger import logger

SWITCH = "EMAIL_SEARCH_V2"


def email_search_v2_enabled() -> bool:
    return os.getenv(SWITCH, "0").strip().lower() in ("1", "true", "yes", "on")


def build_email_card_search(db_client: Any, env_config: Any, embeddings: Any,
                            people: Any) -> Optional[EmailCardSearchService]:
    """The card search over the facts collection, or None when the switch is off; one INFO line says which."""
    if not email_search_v2_enabled():
        logger.info(f"[EmailSearchV2] off ({SWITCH} is not on)")
        return None
    service = EmailCardSearchService(FirestoreEmailCardIndex(db_client, env_config), embeddings, people)
    logger.info("[EmailSearchV2] on")
    return service
