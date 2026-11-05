"""
Email Search Service
====================

Application service encapsulating email-retrieval operations
used by EmailSearchAgent:

  - vector_search(primary_query, alternative_query, tags, user_id)
      7-stream multi-vector RRF across all indexed email vector fields.
  - get_details(email_id, user_id)       — Gmail full body + attachment labels with refs (no bytes)
  - list_attachment_parts(user_id, message_id)   — the message's attachments, one listing (EXTERNAL_FILES_RFC §4.7)
  - get_attachment_part(user_id, message_id, part, max_bytes) — one listed part's bytes, for the open path

Multi-vector search strategy (2 parallel find_nearest calls → RRF merge):
  Call A: vector:embed(primary)     + tags_vector:embed(tags)      + metadata_vector:embed(primary)
  Call B: vector:embed(alternative) + tags_vector:embed(primary)   + metadata_vector:embed(tags)
          + attachments_vector:embed(tags)
Total: 7 search streams, 3 embedding calls (parallel).
"""

import asyncio
from dataclasses import replace
import json
import secrets
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.embedding_service import EmbeddingService
from ..domain.connector_spec import ConnectorSpec
from ..domain.email import EmailPart, IndexedEmail
from ..domain.email_details import EmailDetails
from ..domain.email_open_texts import EMAIL_NOT_CONNECTED_NOTE, MAILBOX_UNAVAILABLE_NOTE
from ..domain.email_ref import email_file_label, format_email_ref
from ..domain.exceptions import EmailMessageGone, EmailNotConnected, EmailPartUnavailable
from ..domain.file_ref import FileRefKind
from ..domain.oauth_credential import OAuthCredential
from ..domain.untrusted_text import EXTERNAL_REMINDER, wrap_untrusted
from ..utils.logger import logger

# RRF constant — same as used in fact repository
_RRF_K = 60


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
        embedding_service: EmbeddingService,
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
        self._embedding = embedding_service

    # ------------------------------------------------------------------
    # Vector search — 7-stream multi-vector RRF
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        primary_query: str,
        alternative_query: str,
        tags: List[str],
        user_id: str,
        limit: int = 10,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> str:
        """
        Embed primary, alternative, and tags → 2 parallel find_nearest calls →
        7 search streams → second-level RRF merge → formatted text.
        date_from / date_to: optional pre-filter on email_date passed through to find_nearest.
        Results are filtered server-side by cosine distance 0.4; all passing results returned.
        `limit` controls per-adapter-call fetch size (Firestore headroom).
        """
        logger.info(
            f"📧 EmailSearchService.vector_search: "
            f"primary='{primary_query[:50]}' alt='{alternative_query[:50]}' "
            f"tags={tags} user={user_id[:8]} "
            f"date_from={date_from} date_to={date_to}"
        )

        tags_text = " ".join(tags) if tags else primary_query

        # Step 1: 3 embedding calls in parallel
        e_primary, e_alternative, e_tags = await asyncio.gather(
            self._embedding.get_embedding(primary_query, task_type="RETRIEVAL_QUERY"),
            self._embedding.get_embedding(alternative_query, task_type="RETRIEVAL_QUERY"),
            self._embedding.get_embedding(tags_text, task_type="RETRIEVAL_QUERY"),
        )

        date_kwargs = {"date_from": date_from, "date_to": date_to}

        # Step 2: 2 find_nearest calls in parallel
        # Call A: 3 streams (primary content + tags category + primary metadata)
        # Call B: 4 streams (alternative content + primary on tags + tags metadata + tags attachments)
        results_a, results_b = await asyncio.gather(
            self._email_repo.find_nearest(
                user_id=user_id,
                vectors={
                    "vector": e_primary,
                    "tags_vector": e_tags,
                    "metadata_vector": e_primary,
                },
                limit=limit * 2,
                **date_kwargs,
            ),
            self._email_repo.find_nearest(
                user_id=user_id,
                vectors={
                    "vector": e_alternative,
                    "tags_vector": e_primary,
                    "metadata_vector": e_tags,
                    "attachments_vector": e_tags,
                },
                limit=limit * 2,
                **date_kwargs,
            ),
        )

        # Step 3: second-level RRF merge — no output cap, return all similarity-passing results
        merged = self._rrf_merge([results_a, results_b])

        logger.info(
            f"📧 EmailSearchService.vector_search done: "
            f"A={len(results_a)} B={len(results_b)} → merged={len(merged)}"
        )

        if not merged:
            return "No emails found matching your query."

        emails = []
        for email in merged:
            emails.append({
                "email_id": email.email_id,
                "from": email.from_address,
                "date": email.email_date.strftime("%Y-%m-%d") if email.email_date else None,
                "text": email.text,
                "attachments": email.attachments or [],
            })

        return json.dumps({"count": len(emails), "emails": emails}, ensure_ascii=False)

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

    @staticmethod
    def _rrf_merge(
        lists: List[List[IndexedEmail]],
        k: int = _RRF_K,
    ) -> List[IndexedEmail]:
        """
        Second-level RRF across multiple find_nearest result lists.
        Each list is already RRF-ranked internally by the adapter.
        Returns all results — no output cap; server-side distance filter applies.
        """
        seen: Dict[str, IndexedEmail] = {}
        scores: Dict[str, float] = {}

        for result_list in lists:
            for rank, email in enumerate(result_list):
                if email.email_id not in seen:
                    seen[email.email_id] = email
                scores[email.email_id] = (
                    scores.get(email.email_id, 0.0) + 1.0 / (k + rank + 1)
                )

        top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
        return [seen[eid] for eid in top_ids]

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
