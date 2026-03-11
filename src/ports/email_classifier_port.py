"""
EmailClassifierPort — classify a batch of email metadata.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §4.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.email import EmailClassificationResult, EmailMetadata, OAuthCredentials


class EmailClassifierPort(ABC):

    @abstractmethod
    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        credentials: Optional[OAuthCredentials] = None,
    ) -> List[EmailClassificationResult]:
        """
        Classify a batch of email metadata.
        Returns one result per input email (including valuable=False).
        credentials: optional, used to fetch full email content for ambiguous cases.
        """
