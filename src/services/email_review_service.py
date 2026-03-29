"""
EmailReviewService
==================

Fetches the last 24h of Gmail emails for a user and builds a structured
payload for SmartAgent daily inbox analysis.

Responsibilities:
  - Token refresh (if expiring within 5 minutes)
  - Paginated email metadata fetch (capped at 200)
  - Full content fetch (body text + attachment names, no binaries)
  - Structured JSON payload assembly
  - System alert framing for SmartAgent
"""

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..domain.email import OAuthCredentials
from ..ports.email_provider_port import EmailProviderPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger


_MAX_EMAILS = 200
_MAX_BODY_CHARS = 500


class EmailReviewService:

    def __init__(
        self,
        email_provider: EmailProviderPort,
        oauth_credentials: OAuthCredentialsPort,
    ) -> None:
        self._email_provider = email_provider
        self._oauth = oauth_credentials

    async def fetch_review_payload(self, user_id: str) -> Optional[List[dict]]:
        """
        Fetch and structure the last 24h of Gmail emails for user_id.
        Returns None if credentials are missing or token refresh fails.
        Returns an empty list if no emails were received in the period.
        """
        creds = await self._oauth.get_credentials(user_id, "gmail")
        if not creds:
            logger.warning(f"[EmailReview] no credentials for {user_id[:8]}")
            return None

        creds = await self._refresh_if_needed(creds)
        if creds is None:
            return None

        now_utc = datetime.now(timezone.utc)
        date_from = now_utc - timedelta(hours=24)

        all_metadata = []
        page_token = None
        while len(all_metadata) < _MAX_EMAILS:
            batch, page_token = await self._email_provider.list_emails(
                credentials=creds,
                date_from=date_from,
                date_to=now_utc,
                page_token=page_token,
                max_results=min(100, _MAX_EMAILS - len(all_metadata)),
            )
            all_metadata.extend(batch)
            if not page_token:
                break

        if not all_metadata:
            return []

        full_content = await self._email_provider.batch_get_full_content(
            credentials=creds,
            email_ids=[m.email_id for m in all_metadata],
            deep=False,
        )

        return [
            {
                "email_id": meta.email_id,
                "from": meta.from_address,
                "subject": meta.subject,
                "date": meta.date.isoformat(),
                "snippet": meta.snippet,
                "body": (
                    full_content[meta.email_id].body_text[:_MAX_BODY_CHARS]
                    if meta.email_id in full_content and full_content[meta.email_id].body_text
                    else ""
                ),
                "attachments": (
                    full_content[meta.email_id].attachments
                    if meta.email_id in full_content
                    else []
                ),
            }
            for meta in all_metadata
        ]

    @staticmethod
    def build_alert(date_str: str, emails: List[dict]) -> str:
        return (
            f"[DAILY EMAIL REVIEW] {date_str}\n"
            f"{len(emails)} emails received in the last 24 hours.\n"
            f"\n"
            f"{json.dumps(emails, ensure_ascii=False, indent=2)}\n"
            f"\n"
            f"---\n"
            f"This is the user's inbox for today. You know who the user is — draw on that knowledge.\n"
            f"The email_id field lets you fetch full content (get_email_details) or attachments "
            f"(get_email_attachment) for anything worth investigating further.\n"
            f"Deliver your findings as an HTML page (create_html_page)."
        )

    async def _refresh_if_needed(self, creds: OAuthCredentials) -> Optional[OAuthCredentials]:
        now_utc = datetime.now(timezone.utc)
        if creds.token_expiry and creds.token_expiry <= now_utc + timedelta(minutes=5):
            try:
                creds = await self._email_provider.refresh_token(creds)
                await self._oauth.save_credentials(creds)
            except Exception as exc:
                logger.warning(
                    f"[EmailReview] token refresh failed for {creds.user_id[:8]}: {exc}"
                )
                return None
        return creds
