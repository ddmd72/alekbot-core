"""Shared setup of the two email-card scripts (`card_probe.py`, `recard_subscription.py`): the owner's Gmail credential,
the confirmed people, the zone, the attachment text service, and one mail fetched into a `MailSource`.

Ids and secrets come from `.env` / the environment, never from code; nothing here writes to a store. Private adapter
methods (`_fetch_message`, `_parse_metadata`, `_parse_payload`, `_recipients`) are used on purpose, as the probe did: a
local script needs the one `format=full` read per mail, and production slice 6 gets the same fields from
`list_emails` + `batch_get_full_content`."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

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
from src.domain.email_card_source import owner_zone  # noqa: E402
from src.domain.exceptions import EmailMessageGone  # noqa: E402
from src.domain.oauth_credential import OAuthCredential  # noqa: E402
from src.ports.content_extractor_port import ContentExtractorPort  # noqa: E402
from src.services.document_service import DocumentService  # noqa: E402
from src.services.email_attachment_text_service import EmailAttachmentTextService  # noqa: E402
from src.services.email_card_service import MailSource  # noqa: E402


class _NoReader(ContentExtractorPort):
    """The paid reader is never reached on the index path (`CALLER_INDEX`); it raises if it ever is."""

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

    @property
    def owner_addresses(self) -> Tuple[str, ...]:
        """The primary address only: the send-as aliases are not confirmed by a live probe yet (card D7)."""
        return (self.mailbox,) if self.mailbox else ()


async def open_session(user_id: Optional[str] = None, account_id: Optional[str] = None) -> CardSession:
    """Connect as the dev owner: ids from the arguments, else `DEV_USER_ID` / `DEV_ACCOUNT_ID` of `.env`."""
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
    attachments = EmailAttachmentTextService(gmail, DocumentService(extractor, _NoReader(), extractor))
    return CardSession(user_id, account_id, credential, gmail, mailbox, zone, people, attachments)


async def fetch_source(session: CardSession, email_id: str) -> Tuple[Optional[MailSource], str]:
    """One `format=full` read of one mail: (source, "") or (None, "gone" | "unavailable")."""
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
