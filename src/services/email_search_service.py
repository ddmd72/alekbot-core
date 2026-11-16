"""
Email Search Service
====================

Application service encapsulating email-retrieval operations
used by EmailSearchAgent:

  - list_headers(user_id, ...) / index_watermark(user_id) — the live headers newer than the index
  - get_details(email_id, user_id)       — Gmail full body + attachment labels with refs (no bytes)
  - list_attachment_parts(user_id, message_id)   — the message's attachments, one listing (EXTERNAL_FILES_RFC §4.7)
  - get_attachment_part(user_id, message_id, part, max_bytes) — one listed part's bytes, for the open path
"""

import asyncio
from dataclasses import replace
import secrets
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.email_provider_port import EmailProviderPort
from ..domain.connector_spec import ConnectorSpec
from ..domain.email import GMAIL_INDEX_PROVIDER, EmailMetadata, EmailPart
from ..domain.email_details import EmailDetails
from ..domain.email_open_texts import EMAIL_NOT_CONNECTED_NOTE, MAILBOX_UNAVAILABLE_NOTE
from ..domain.email_ref import email_file_label, format_email_ref
from ..domain.exceptions import EmailMessageGone, EmailNotConnected, EmailPartUnavailable
from ..domain.file_ref import FileRefKind
from ..domain.oauth_credential import OAuthCredential
from ..domain.untrusted_text import EXTERNAL_REMINDER, wrap_untrusted
from ..utils.logger import logger

def _nonce() -> str:
    """A fresh nonce per wrapped body, as the drive draws one per result (EXTERNAL_FILES_RFC §4.6)."""
    return secrets.token_hex(8)


class EmailSearchService:
    """
    Orchestrates email retrieval for EmailSearchAgent.
    All I/O via injected ports — no infrastructure imports.
    """

    def __init__(
        self,
        indexed_email_repo: IndexedEmailRepository,
        oauth_credentials: OAuthCredentialsPort,
        gmail_provider: EmailProviderPort,
        *,
        connector: ConnectorSpec,
        nonce: Callable[[], str] = _nonce,
    ) -> None:
        """`connector` is the mail kind's spec: the user's Gmail credential is the one of `connector.kind`.
        `nonce` tags the untrusted wrapper of each body `get_details` returns."""
        self._nonce = nonce
        self._email_repo = indexed_email_repo
        self._oauth = oauth_credentials
        self._kind = connector.kind
        self._gmail = gmail_provider

    # ------------------------------------------------------------------
    # Live headers (mail newer than the index, exact keys)
    # ------------------------------------------------------------------

    async def list_headers(
        self,
        user_id: str,
        *,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
        key: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[EmailMetadata], bool]:
        """Live headers from the mailbox (port `list_headers`), under the user's refreshed credential.
        Raises EmailNotConnected without a usable credential."""
        creds = await self._connected_credential(user_id)
        return await self._gmail.list_headers(creds, after=after, before=before, key=key, limit=limit)

    async def index_watermark(self, user_id: str) -> Optional[datetime]:
        """The newest mail date the incremental indexing has seen, as an aware UTC datetime; None when the
        indexing never ran."""
        state = await self._email_repo.get_indexing_state(user_id, GMAIL_INDEX_PROVIDER)
        through = state.indexed_through if state else None
        if through is None:
            return None
        return through.replace(tzinfo=timezone.utc) if through.tzinfo is None else through.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Get full email body
    # ------------------------------------------------------------------

    async def get_details(self, email_id: str, user_id: str) -> str:
        """The text of `get_details_with_parts`, for callers that need no attachment refs."""
        return (await self.get_details_with_parts(email_id, user_id)).text

    async def get_details_with_parts(self, email_id: str, user_id: str) -> EmailDetails:
        """
        Fetch full email body from Gmail (no attachment bytes) and list its attachments once (D11).
        Returns the text (or an error message) and the attachments as `(label, ref)` pairs.

        The body is the sender's text, so it goes inside one `<external_content>` wrapper (EXTERNAL_FILES_RFC §4.6, P14)
        after its reminder; the tool's own frame (the id line) stays outside. The wrapper's name is empty: this fetch
        carries no subject (`EmailFullContent` has none). The attachments follow the wrapper as system-made labels
        carrying their refs (plan U4 §6): a label's name is escaped, so a forged name cannot end a quote or open a tag.
        When the listing fails after the body was read, the body is still returned, with the attachment names the
        index-side fetch knows (inside the wrapper, as before) and the mailbox note in place of labels.
        """
        logger.info(f"📧 EmailSearchService.get_details: {email_id} user={user_id[:8]}")

        creds = await self._credential(user_id)
        if not creds:
            return EmailDetails(EMAIL_NOT_CONNECTED_NOTE)

        creds = await self._maybe_refresh(creds)
        if creds is None:
            return EmailDetails(EMAIL_NOT_CONNECTED_NOTE)
        contents, listed = await asyncio.gather(
            self._gmail.batch_get_full_content(creds, [email_id]),
            self._list_parts_or_error(creds, email_id),
        )
        email = contents.get(email_id)
        if not email or listed is EmailMessageGone:
            return EmailDetails(f"[Error: Email {email_id} not found or inaccessible.]")

        body_preview = (email.body_text or "")[:5000]
        if listed is EmailPartUnavailable:
            names = f"\nAttachments: {', '.join(email.attachments)}" if email.attachments else ""
            wrapped = wrap_untrusted(body_preview + names, nonce=self._nonce(), store=FileRefKind.EMAIL,
                                     ref=email_id, label="")
            note = f"\n{MAILBOX_UNAVAILABLE_NOTE}" if email.attachments else ""
            return EmailDetails(f"{EXTERNAL_REMINDER}\nEmail {email_id}:\nBody:\n{wrapped}{note}")

        wrapped = wrap_untrusted(body_preview, nonce=self._nonce(), store=FileRefKind.EMAIL, ref=email_id, label="")
        files = self._labels(email_id, listed)
        labels = "".join(f"\n{label}" for label, _ in files)
        heading = "\nAttachments:" if files else ""
        return EmailDetails(f"{EXTERNAL_REMINDER}\nEmail {email_id}:\nBody:\n{wrapped}{heading}{labels}", tuple(files))

    async def _list_parts_or_error(self, creds, message_id: str):
        """The listing, or the exception class that says why there is none (the body fetch still answers)."""
        try:
            return await self._gmail.list_parts(creds, message_id)
        except EmailMessageGone:
            return EmailMessageGone
        except EmailPartUnavailable:
            logger.warning(f"📧 EmailSearchService: attachment listing of {message_id} unavailable")
            return EmailPartUnavailable

    @staticmethod
    def _labels(message_id: str, parts: List[EmailPart]) -> List[Tuple[str, str]]:
        """`(label, ref)` per part; a part whose ids do not fit the ref shape is left out, logged."""
        out: List[Tuple[str, str]] = []
        for part in parts:
            try:
                ref = format_email_ref(message_id, part.part_id)
                out.append((email_file_label(part.filename, part.size, part.mime, ref), ref))
            except ValueError:
                logger.warning(f"📧 EmailSearchService: part {part.part_id!r} of {message_id} has no valid ref")
        return out

    # ------------------------------------------------------------------
    # Part-level fetch for the open path (EXTERNAL_FILES_RFC §4.7 P8a)
    # ------------------------------------------------------------------

    async def list_attachment_parts(self, user_id: str, message_id: str) -> List[EmailPart]:
        """
        The attachments of one message, from one provider listing, under the user's mail credential (refreshed when
        expired, as `get_details`). Raises EmailNotConnected without a credential or when the refresh finds it
        disconnected; the port's EmailMessageGone / EmailPartUnavailable and OAuth errors propagate.
        """
        creds = await self._connected_credential(user_id)
        return await self._gmail.list_parts(creds, message_id)

    async def get_attachment_part(self, user_id: str, message_id: str, part: EmailPart, max_bytes: int) -> bytes:
        """
        The bytes of `part`, which `list_attachment_parts` just listed for `message_id` (its handle is valid only
        with that listing), refused past `max_bytes`. Credential and errors as `list_attachment_parts`, plus the
        port's EmailPartTooLarge.
        """
        creds = await self._connected_credential(user_id)
        return await self._gmail.get_part(creds, message_id, part, max_bytes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _credential(self, user_id: str) -> Optional[OAuthCredential]:
        """The user's credential of the mail kind (the oldest when several), or None."""
        keys = await self._oauth.list_keys(user_id, self._kind)
        return await self._oauth.get(user_id, keys[0].credential_id) if keys else None

    async def _connected_credential(self, user_id: str) -> OAuthCredential:
        """The user's refreshed mail credential; EmailNotConnected when there is none or the refresh found it
        disconnected."""
        creds = await self._credential(user_id)
        if creds:
            creds = await self._maybe_refresh(creds)
        if not creds:
            raise EmailNotConnected(user_id)
        return creds

    async def _maybe_refresh(self, creds):
        """Refresh access token if expired."""
        now = datetime.now(timezone.utc)
        expiry = creds.token_expiry
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry and expiry <= now:
            logger.info("📧 EmailSearchService: refreshing expired Gmail token")
            # the refreshed record is the one read (D-P2): its credential_id keys the update
            refreshed = await self._gmail.refresh_token(creds)
            creds = (replace(refreshed, credential_id=creds.credential_id)
                     if creds.credential_id and not refreshed.credential_id else refreshed)
            if not await self._oauth.update(creds):
                logger.info("📧 EmailSearchService: Gmail disconnected during token refresh")
                return None
        return creds
