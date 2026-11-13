"""Wiring of the online email card step (email index v2 §6.5; slice 6).

New mail the indexer keeps also gets its v2 card, stored on the same document after the v1 save. Behind the optional
knob `EMAIL_CARD_ONLINE` (read here with `os.getenv`, never in `load_settings()`): **default off**; `1` switches it on.
The step shares the graph's one document service, mailbox provider and embedding adapter; without a document service
(no file conversion) there is no step.
"""
import os
from typing import Any

from ..adapters.firestore_email_card_store import FirestoreEmailCardStore
from ..services.email_attachment_text_service import EmailAttachmentTextService
from ..services.email_card_indexer import EmailCardIndexer
from ..services.email_card_service import EmailCardService, MailSource
from ..services.prompt_builder import PromptBuilder
from ..utils.logger import logger
from .email_card_runner import EmailCardRunner, card_writer_model

SWITCH = "EMAIL_CARD_ONLINE"


def email_card_online_enabled() -> bool:
    return os.getenv(SWITCH, "0").strip().lower() in ("1", "true", "yes", "on")


def attach_email_card_step(email_indexing_service: Any, container: Any, documents: Any, db_client: Any,
                           env_config: Any, user_repo: Any, quota_service: Any) -> bool:
    """Build the card step and attach it to the indexing service; False (with one INFO line saying why) when not."""
    if not email_card_online_enabled():
        logger.info(f"[EmailCard] online carding off ({SWITCH} is not on)")
        return False
    if documents is None:
        logger.info("[EmailCard] online carding off (no document service)")
        return False
    runner = EmailCardRunner(
        context_builder=container.context_builder,
        prompt_builder=PromptBuilder(repo=None, assembly_service=container.assembly_service),
        model=card_writer_model(os.getenv("EMAIL_CARD_MODEL")), quota_service=quota_service)
    service = EmailCardService(container.gmail_provider,
                               EmailAttachmentTextService(container.gmail_provider, documents),
                               runner, container.embedding_service)
    email_indexing_service.attach_card_step(EmailCardIndexer(
        service, FirestoreEmailCardStore(db_client, env_config), container.confirmed_people, user_repo,
        quota=quota_service, source_factory=MailSource))
    logger.info("[EmailCard] online carding on")
    return True
