"""
Email Search Service
====================

Application service encapsulating email-retrieval operations
used by EmailSearchAgent:

  - vector_search(primary_query, alternative_query, tags, user_id)
      7-stream multi-vector RRF across all indexed email vector fields.
  - get_details(email_id, user_id)       — Gmail full body (no attachments)
  - get_attachment(email_id, filename, user_id) — Gmail attachment → text

Multi-vector search strategy (2 parallel find_nearest calls → RRF merge):
  Call A: vector:embed(primary)     + tags_vector:embed(tags)      + metadata_vector:embed(primary)
  Call B: vector:embed(alternative) + tags_vector:embed(primary)   + metadata_vector:embed(tags)
          + attachments_vector:embed(tags)
Total: 7 search streams, 3 embedding calls (parallel).
"""

import asyncio
import json
import mimetypes
import tempfile
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.embedding_service import EmbeddingService
from ..domain.email import IndexedEmail
from ..services.file_conversion_service import (
    convert_file_to_text,
    truncate_with_alert,
)
from ..utils.logger import logger

# Per-attachment and total size guards
_MAX_SINGLE_BYTES = 3 * 1024 * 1024   # 3 MB
_MAX_TOTAL_BYTES  = 10 * 1024 * 1024  # 10 MB

# RRF constant — same as used in fact repository
_RRF_K = 60


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
    ) -> None:
        self._email_repo = indexed_email_repo
        self._oauth = oauth_credentials
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
        """
        Fetch full email body from Gmail (no attachments).
        Returns formatted text or an error message.
        """
        logger.info(f"📧 EmailSearchService.get_details: {email_id} user={user_id[:8]}")

        creds = await self._oauth.get_credentials(user_id, "gmail")
        if not creds:
            return "[Error: Gmail is not connected for this user. Ask the user to reconnect via /link-gmail.]"

        creds = await self._maybe_refresh(creds)
        contents = await self._gmail.batch_get_full_content(creds, [email_id], deep=False)
        email = contents.get(email_id)
        if not email:
            return f"[Error: Email {email_id} not found or inaccessible.]"

        body_preview = (email.body_text or "")[:5000]
        attachments_str = (
            f"\nAttachments: {', '.join(email.attachments)}" if email.attachments else ""
        )
        return (
            f"Email {email_id}:\n"
            f"Body:\n{body_preview}"
            + attachments_str
        )

    # ------------------------------------------------------------------
    # Fetch + convert attachment
    # ------------------------------------------------------------------

    async def get_attachment(
        self,
        email_id: str,
        filename: str,
        user_id: str,
    ) -> str:
        """
        Download attachment from Gmail and convert to text.
        Enforces per-file (3 MB) and total (10 MB) size limits.
        """
        logger.info(
            f"📧 EmailSearchService.get_attachment: {email_id}/{filename} user={user_id[:8]}"
        )

        creds = await self._oauth.get_credentials(user_id, "gmail")
        if not creds:
            return "[Error: Gmail is not connected for this user.]"

        creds = await self._maybe_refresh(creds)
        contents = await self._gmail.batch_get_full_content(creds, [email_id], deep=True)
        email = contents.get(email_id)
        if not email:
            return f"[Error: Email {email_id} not found or inaccessible.]"

        raw_bytes = email.attachment_binaries.get(filename)
        if raw_bytes is None:
            available = ", ".join(email.attachment_binaries.keys()) or "none"
            return f"[Error: Attachment '{filename}' not found. Available: {available}]"

        size = len(raw_bytes)
        if size > _MAX_SINGLE_BYTES:
            mb = size / 1024 / 1024
            return (
                f"[Error: Attachment '{filename}' is {mb:.1f} MB — exceeds the 3 MB limit. "
                f"Cannot include it in context.]"
            )
        if size > _MAX_TOTAL_BYTES:
            return (
                f"[Error: Total attachment size exceeds the 10 MB limit. "
                f"Cannot include '{filename}'.]"
            )

        mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or "application/octet-stream"

        suffix = os.path.splitext(filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name

        try:
            text = await convert_file_to_text(
                local_path=tmp_path,
                filename=filename,
                mime_type=mime_type,
                audio_service=None,
            )
        finally:
            os.unlink(tmp_path)

        return truncate_with_alert(text, filename)

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

    async def _maybe_refresh(self, creds):
        """Refresh access token if expired."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expiry = creds.token_expiry
        if expiry and expiry <= now:
            logger.info("📧 EmailSearchService: refreshing expired Gmail token")
            creds = await self._gmail.refresh_token(creds)
            await self._oauth.save_credentials(creds)
        return creds
