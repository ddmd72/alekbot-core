"""Shared setup of the two email-card scripts (`card_probe.py`, `recard_subscription.py`): the owner's Gmail credential,
the confirmed people, the zone, the attachment text service, and one mail fetched into a `MailSource`.

Ids and secrets come from `.env` / the environment, never from code; nothing here writes to a store. Private adapter
methods (`_fetch_message`, `_parse_metadata`, `_parse_payload`, `_recipients`) are used on purpose, as the probe did: a
local script needs the one `format=full` read per mail, and production slice 6 gets the same fields from
`list_emails` + `batch_get_full_content`.

Scans (slice 5): `reader_document_service` is the main service's reader over a local parser, `SpendCap` the one money cap
of a run (reader plus writer or embeddings) and `MeteredAttachmentText` settles it with what the reader reported."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts import _firestore  # noqa: E402
from src.adapters.firestore_account_repo import FirestoreAccountRepository  # noqa: E402
from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository  # noqa: E402
from src.adapters.firestore_credential_store import FirestoreCredentialStore  # noqa: E402
from src.adapters.firestore_user_repo import FirestoreUserRepository  # noqa: E402
from src.adapters.gmail_provider_adapter import GmailProviderAdapter  # noqa: E402
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor  # noqa: E402
from src.composition import cipher_setup  # noqa: E402
from src.config.environment import EnvironmentConfig  # noqa: E402
from src.domain.confirmed_people import ConfirmedPeople  # noqa: E402
from src.domain.drive_index_params import READER_PAGE_USD_ESTIMATE  # noqa: E402
from src.domain.email import EmailPart  # noqa: E402
from src.domain.email_attachment import AttachmentRead, EmailAttachment  # noqa: E402
from src.domain.email_card_source import owner_zone  # noqa: E402
from src.domain.exceptions import EmailMessageGone  # noqa: E402
from src.domain.oauth_credential import OAuthCredential  # noqa: E402
from src.ports.content_extractor_port import ContentExtractorPort  # noqa: E402
from src.ports.document_read_port import DocumentReadPort  # noqa: E402
from src.ports.reader_page_budget import ReaderPageBudget  # noqa: E402
from src.services.document_service import DocumentService  # noqa: E402
from src.services.email_attachment_text_service import EmailAttachmentTextService  # noqa: E402
from src.services.email_card_service import MailSource  # noqa: E402


class _NoReader(ContentExtractorPort):
    """The no-reader service of a run with no reader pages: the paid reader is never reached (a `CALLER_INDEX` read
    without `reader_pages` skips it); it raises if it ever is."""

    @property
    def extractor_id(self) -> str:
        return "no_reader"

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: Any) -> Any:
        raise RuntimeError("the reader is not available in the card scripts")


@dataclass
class CardSession:
    user_id: str
    account_id: str
    credential: OAuthCredential
    gmail: GmailProviderAdapter
    mailbox: str
    zone: str
    people: Optional[ConfirmedPeople]
    attachments: EmailAttachmentTextService
    stored: Optional[OAuthCredential] = None       # the stored credential, refreshed again before the access token expires
    refreshed_at: float = 0.0
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    documents: Optional[DocumentReadPort] = None     # what `attachments` reads through (a script wraps it in `MeteredAttachmentText`)

    @property
    def owner_addresses(self) -> Tuple[str, ...]:
        """The primary address only: the send-as aliases are not confirmed by a live probe yet (card D7)."""
        return (self.mailbox,) if self.mailbox else ()


class SpendCap:
    """Reserve before a call, settle after it: spent + reserved + the next projection never passes the cap."""

    def __init__(self, cap: float) -> None:
        self.cap, self.spent, self.reserved = cap, 0.0, 0.0
        self._lock = asyncio.Lock()

    async def reserve(self, projected: float) -> bool:
        async with self._lock:
            if self.spent + self.reserved + projected > self.cap:
                return False
            self.reserved += projected
            return True

    async def settle(self, projected: float, actual: float) -> None:
        async with self._lock:
            self.reserved -= projected
            self.spent += actual


class MeteredAttachmentText(EmailAttachmentTextService):
    """The attachment service of a run that pays for the reader. The budget's gate reserves `pages x
    READER_PAGE_USD_ESTIMATE` on the cap before a scan is read; here every attachment that was booked is settled with
    what the reader reported. A read that raised leaves its reservation in place (the run's cap stays conservative).
    Totals for the run's summary line."""

    def __init__(self, provider: Any, documents: DocumentReadPort, cap: SpendCap, **kwargs: Any) -> None:
        super().__init__(provider, documents, **kwargs)
        self._cap = cap
        self.reader_pages = 0
        self.reader_usd = 0.0
        self.scans_read = 0
        self.scans_not_read = 0
        self.mails_with_scans = 0
        self.by_mail: Dict[str, Tuple[int, float]] = {}   # message id -> (reader pages, reader usd) of its last read

    async def read_attachments(self, credentials: OAuthCredential, message_id: str, parts: Sequence[EmailPart], *,
                               account_id: str, deadline_at: float, cache: MutableMapping[str, EmailAttachment],
                               reader_budget: Optional[ReaderPageBudget] = None) -> Tuple[EmailAttachment, ...]:
        result = await super().read_attachments(credentials, message_id, parts, account_id=account_id,
                                                deadline_at=deadline_at, cache=cache, reader_budget=reader_budget)
        scans = False
        for a in result:
            if a.reader_pages:
                await self._cap.settle(a.reader_pages * READER_PAGE_USD_ESTIMATE, a.reader_usd)
                self.reader_pages += a.reader_pages
                self.reader_usd += a.reader_usd
            if a.read is AttachmentRead.READ_BY_READER:
                self.scans_read += 1
                scans = True
            elif a.read is AttachmentRead.SCAN_NOT_READ:
                self.scans_not_read += 1
                scans = True
        self.mails_with_scans += int(scans)
        self.by_mail[message_id] = (sum(a.reader_pages for a in result), sum(a.reader_usd for a in result))
        return result

    def summary(self) -> str:
        mean = self.reader_usd / self.mails_with_scans if self.mails_with_scans else 0.0
        return (f"scans_read={self.scans_read} scans_not_read={self.scans_not_read} reader_pages={self.reader_pages} "
                f"reader_usd=${self.reader_usd:.4f} mean_reader_usd_per_scanned_mail=${mean:.5f}")


def page_gate(cap: SpendCap) -> Callable[[int], Awaitable[bool]]:
    """The dollar check of a `JobReaderPageBudget`: reserve the estimate of `pages` on the run's cap."""
    async def gate(pages: int) -> bool:
        return await cap.reserve(pages * READER_PAGE_USD_ESTIMATE)
    return gate


def reader_document_service(container: Any = None) -> DocumentService:
    """The main service's reader over a local parser: the document service as `build_application_graph` makes it, with
    the parser child in this process (`SubprocessContentExtractor`) instead of the sandbox. Billing goes to the owner's
    account through `FirestoreQuotaService`, as the reader's own ledger does online. `container` is a built
    `ServiceContainer`; None builds one."""
    from src.composition.document_service_setup import build_document_service
    from src.composition.service_container import ServiceContainer
    from src.adapters.firestore_quota_service import FirestoreQuotaService
    from src.config.settings import load_settings

    db = _firestore.async_client()
    if container is None:
        config = load_settings()
        env_config = config["ENVIRONMENT_CONFIG"]
        accounts = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
        container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=accounts)
    else:
        env_config = EnvironmentConfig()
        accounts = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    users = FirestoreUserRepository(db, env_config, accounts)
    return build_document_service(
        context_builder=container.context_builder, users=users, assembly_service=container.assembly_service,
        quota=FirestoreQuotaService(accounts), transcriber=None, transcript_cache=None,
        parser=SubprocessContentExtractor())


async def open_session(user_id: Optional[str] = None, account_id: Optional[str] = None, *,
                       documents: Optional[DocumentReadPort] = None) -> CardSession:
    """Connect as the dev owner: ids from the arguments, else `DEV_USER_ID` / `DEV_ACCOUNT_ID` of `.env`. `documents`
    None keeps the no-reader document service; a script that reads scans passes `reader_document_service()`."""
    user_id = user_id or os.environ["DEV_USER_ID"]
    account_id = account_id or os.environ["DEV_ACCOUNT_ID"]
    env_config = EnvironmentConfig()
    db = _firestore.async_client()
    store = FirestoreCredentialStore(db, env_config, cipher=cipher_setup.build_cipher(os.environ["KMS_CRYPTO_KEY"]))
    gmail = GmailProviderAdapter(os.environ["GOOGLE_OAUTH_CLIENT_ID"], os.environ["GOOGLE_OAUTH_CLIENT_SECRET"])
    keys = await store.list_keys(user_id, "gmail")
    if not keys:
        raise SystemExit("no Gmail credential for the dev user")
    stored = await store.get(user_id, keys[0].credential_id)
    if stored is None:
        raise SystemExit("the Gmail credential could not be read")
    credential = await gmail.refresh_token(stored)
    mailbox = (getattr(credential.account, "label", "") or "").strip()
    accounts = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    people = await FirestoreConfirmedPeopleRepository(db, env_config).get(account_id)   # read only
    profile = await FirestoreUserRepository(db, env_config, accounts).get_user(user_id)   # read only
    zone = owner_zone(profile.config.timezone if profile is not None else None)
    extractor = SubprocessContentExtractor()
    documents = documents if documents is not None else DocumentService(extractor, _NoReader(), extractor)
    attachments = EmailAttachmentTextService(gmail, documents)
    return CardSession(user_id, account_id, credential, gmail, mailbox, zone, people, attachments,
                       stored=stored, refreshed_at=time.monotonic(), documents=documents)


# A Gmail access token lives about an hour; a run of thousands of mails outlives it (a 2026-11 run lost most of its
# mail to 401 after the first hour). Refresh well before that.
REFRESH_AFTER_S = 40 * 60


async def keep_fresh(session: CardSession, now: Optional[float] = None) -> None:
    """Refresh the session's access token once it is older than REFRESH_AFTER_S; one refresh for concurrent callers."""
    if session.stored is None:
        return
    async with session.refresh_lock:
        current = time.monotonic() if now is None else now
        if current - session.refreshed_at < REFRESH_AFTER_S:
            return
        session.credential = await session.gmail.refresh_token(session.stored)
        session.refreshed_at = current


async def fetch_source(session: CardSession, email_id: str) -> Tuple[Optional[MailSource], str]:
    """One `format=full` read of one mail: (source, "") or (None, "gone" | "unavailable")."""
    await keep_fresh(session)
    try:
        msg = await session.gmail._fetch_message(session.credential, email_id)
    except EmailMessageGone:
        return None, "gone"
    except Exception:
        return None, "unavailable"
    payload = msg.get("payload", {})
    meta = session.gmail._parse_metadata(msg)
    body, _html, names = session.gmail._parse_payload(payload)
    names_list: List[str] = list(names)
    return MailSource(email_id=email_id, from_header=meta.from_address, subject=meta.subject, sent_at=meta.date,
                      recipients=session.gmail._recipients(payload), body=body,
                      attachment_names=tuple(names_list)), ""


def read_ids(path: Path, limit: int = 0) -> List[str]:
    """One Gmail id per line; blank lines and `#` comments skipped."""
    ids = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.lstrip().startswith("#")]
    return ids[:limit] if limit else ids
