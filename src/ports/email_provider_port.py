"""
EmailProviderPort — fetch email data from any provider.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.domain.email import EmailFullContent, EmailMetadata, OAuthCredentials


class EmailProviderPort(ABC):

    @abstractmethod
    async def list_emails(
        self,
        credentials: OAuthCredentials,
        date_from: Optional[datetime] = None,
        page_token: Optional[str] = None,
        max_results: int = 100,
    ) -> Tuple[List[EmailMetadata], Optional[str]]:
        """
        Fetch one page of email metadata (subject, from, date, snippet, labels).
        Returns (emails, next_page_token). next_page_token=None means last page.
        date_from=None means no lower bound (full history).
        max_results=100 aligns with one LLM classification batch.
        """

    @abstractmethod
    async def batch_get_full_content(
        self,
        credentials: OAuthCredentials,
        email_ids: List[str],
        deep: bool = False,
    ) -> Dict[str, EmailFullContent]:
        """
        Fetch full content for a list of email IDs in parallel (semaphore=10 in adapter).
        deep=False: body_text + attachment filenames only (attachment_binaries={}).
        deep=True:  also fetches attachment binaries (for markitdown parsing in Mode B).
        Missing or inaccessible IDs are silently absent from result dict.
        """

    @abstractmethod
    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        """
        Exchange refresh_token for a new access_token.
        Returns updated OAuthCredentials with new access_token + token_expiry.
        Raises OAuthExpiredError if refresh_token is expired or revoked.
        """
