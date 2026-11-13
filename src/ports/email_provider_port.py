"""
EmailProviderPort — fetch email data from any provider.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2; the part-level fetch (`list_parts`, `get_part`) is
docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.7 P8a.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.domain.email import EmailFullContent, EmailMetadata, EmailPart
from src.domain.oauth_credential import OAuthCredential


class EmailProviderPort(ABC):

    @abstractmethod
    async def list_emails(
        self,
        credentials: OAuthCredential,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page_token: Optional[str] = None,
        max_results: int = 100,
        query: Optional[str] = None,
        exact_instants: bool = False,
    ) -> Tuple[List[EmailMetadata], Optional[str]]:
        """
        Fetch one page of email metadata (subject, from, date, snippet, labels).
        Returns (emails, next_page_token). next_page_token=None means last page.
        date_from=None means no lower bound. date_to=None means no upper bound.
        query=None means no Gmail search filter (full mailbox).
        max_results=100 aligns with one LLM classification batch.
        exact_instants=False: the bounds are calendar dates (day cursors; the provider may read a date in its own
        zone). True: the bounds are exact instants (a rolling window such as "the last 24 hours").
        """

    @abstractmethod
    async def list_headers(
        self,
        credentials: OAuthCredential,
        *,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
        key: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[EmailMetadata], bool]:
        """
        Message headers (subject, from, date, snippet), the newest first; never promotions, spam or trash.
        `after` / `before` are exact instants (aware datetimes; a naive one is read as UTC). `key` is matched
        literally as a phrase anywhere in the message. Returns (emails, more): `more` says further messages match
        beyond `limit`.
        """

    @abstractmethod
    async def batch_get_full_content(
        self,
        credentials: OAuthCredential,
        email_ids: List[str],
    ) -> Dict[str, EmailFullContent]:
        """
        Fetch full content for a list of email IDs in parallel (semaphore=10 in adapter).
        Returns body_text + attachment filenames only; attachment bytes come from `get_part`.
        Missing or inaccessible IDs are silently absent from result dict.
        """

    @abstractmethod
    async def refresh_token(self, credentials: OAuthCredential) -> OAuthCredential:
        """
        Exchange refresh_token for a new access_token.
        Returns updated OAuthCredential with new access_token + token_expiry.
        Raises OAuthExpiredError if refresh_token is expired or revoked.
        """

    @abstractmethod
    async def list_parts(self, credentials: OAuthCredential, message_id: str) -> List[EmailPart]:
        """
        The message's attachments, from one read of its whole MIME tree, in tree order. One listing rule, the same
        walk that names the attachments `batch_get_full_content` returns (the index stores those names): a part with
        a filename is an attachment, inline images with a Content-ID included; a forwarded `message/rfc822` part is
        one attachment and its children are not listed. Each part's `handle` is valid only with this listing.
        Each part carries `displayed_inline` from its own headers; the listing itself is unchanged (inline parts stay
        listed).
        Raises EmailMessageGone when the message does not exist (404), EmailPartUnavailable when the provider did
        not answer (429, 5xx, timeout), and the OAuth errors a revoked grant or a missing scope raises today.
        """

    @abstractmethod
    async def get_part(self, credentials: OAuthCredential, message_id: str, part: EmailPart,
                       max_bytes: int) -> bytes:
        """
        The bytes of one part `list_parts` just listed for `message_id` (its `handle` locates it; nothing else of
        the message is downloaded). b"" for a part with neither an attachment id nor inline data. Raises
        EmailPartTooLarge when the bytes pass `max_bytes` (the listed size understated them), EmailMessageGone when
        the message or the attachment is gone (404), EmailPartUnavailable on 429, 5xx or a timeout.
        """
