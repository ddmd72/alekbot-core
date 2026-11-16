"""
EmailClassifierPort — the email gate: keep or drop each mail of a listed page.
See docs/10_rfcs/EMAIL_INDEX_V2_RFC.md §4.
"""

from abc import ABC, abstractmethod
from typing import List

from src.domain.email import EmailClassificationResult, EmailMetadata


class EmailClassifierPort(ABC):

    @abstractmethod
    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        *,
        account_id: str,
    ) -> List[EmailClassificationResult]:
        """
        Decide keep or drop for every email of the batch, from its headers and snippet only.
        Returns one result per input email, in input order (dropped and failed ones included).
        account_id: the account whose mail is indexed (the indexing job's); the model calls are billed to it and
        recorded under it.
        """
