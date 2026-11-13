"""
Email card reply — what the card writer returns for one mail (email index v2, slice 3): a status, the parsed answer when
there is one, the model that served and what the calls cost.

The status enum and the reply are one contract and share this module on purpose (one-class rule waived; precedent
`fact_profiler_reply`).

- `ok`: `answer` is the parsed `MailAnswer`.
- `invalid`: the model answered but the answer stayed unusable after the parse retry, or stopped at the output limit.
  This mail's charged failure (the store counts it).
- `refused`: the provider or the model declined this mail's content. This mail's charged failure.
- `unavailable`: the model cannot be used now for anyone (rate limit, outage, timeout, account, a missing prompt,
  anything unexpected). An outage: never counted against the mail.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .email_card import MailAnswer


class CardWriterStatus(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CardWriterReply:
    status: CardWriterStatus
    answer: Optional[MailAnswer]
    model: str    # the model that served (or was asked)
    usd: float    # the calls' cost (billed when a quota service is wired)
    calls: int    # model calls, the parse retry included

    @staticmethod
    def unavailable(model: str = "") -> "CardWriterReply":
        """An outage before any call was made."""
        return CardWriterReply(status=CardWriterStatus.UNAVAILABLE, answer=None, model=model, usd=0.0, calls=0)
