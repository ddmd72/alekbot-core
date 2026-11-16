"""Wiring of the online email card step (email index v2 §6.5; slice 6).

New mail the indexer keeps also gets its v2 card, stored on the same document after the v1 save. Behind the optional
knob `EMAIL_CARD_ONLINE` (read here with `os.getenv`, never in `load_settings()`): **default off**; `1` switches it on.
Scanned PDFs are read by the paid reader under a per-user daily page cap: the optional knob
`EMAIL_READER_PAGES_PER_DAY` (read here with `os.getenv`; default 40; `0` turns the scan reader off online).
The step shares the graph's one document service, mailbox provider and embedding adapter; without a document service
(no file conversion) there is no step.
"""
import os
from typing import Any, Optional

from ..adapters.firestore_email_card_store import FirestoreEmailCardStore
from ..adapters.firestore_email_reader_budget import FirestoreEmailReaderBudget
from ..domain.email_attachment_policy import EMAIL_READER_PAGES_PER_DAY_DEFAULT
from ..ports.reader_page_budget import ReaderPageBudget
from ..services.email_attachment_text_service import EmailAttachmentTextService
from ..services.email_card_indexer import EmailCardIndexer
from ..services.email_card_service import EmailCardService, MailSource
from ..services.prompt_builder import PromptBuilder
from ..utils.logger import logger
from .email_card_runner import EmailCardRunner, card_writer_model

SWITCH = "EMAIL_CARD_ONLINE"
READER_PAGES_KNOB = "EMAIL_READER_PAGES_PER_DAY"


def email_card_online_enabled() -> bool:
    return os.getenv(SWITCH, "0").strip().lower() in ("1", "true", "yes", "on")


def reader_pages_per_day() -> int:
    """The daily reader page cap: the knob, or the default (with one WARNING) when it is not an integer."""
    raw = os.getenv(READER_PAGES_KNOB)
    if raw is None or not raw.strip():
        return EMAIL_READER_PAGES_PER_DAY_DEFAULT
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(f"[EmailCard] {READER_PAGES_KNOB} is not an integer; using {EMAIL_READER_PAGES_PER_DAY_DEFAULT}")
        return EMAIL_READER_PAGES_PER_DAY_DEFAULT


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
    pages = reader_pages_per_day()
    budget: Optional[ReaderPageBudget] = None
    if pages > 0:
        budget = FirestoreEmailReaderBudget(db_client, env_config, pages)
    else:
        logger.info(f"[EmailCard] scan reader off ({READER_PAGES_KNOB} is {pages})")
    email_indexing_service.attach_card_step(EmailCardIndexer(
        service, FirestoreEmailCardStore(db_client, env_config), container.confirmed_people, user_repo,
        quota=quota_service, source_factory=MailSource, reader_budget=budget))
    logger.info("[EmailCard] online carding on")
    return True
