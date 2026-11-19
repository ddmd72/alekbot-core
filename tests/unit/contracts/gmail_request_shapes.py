"""The request shapes GmailProviderAdapter sends, for the request-conformance test (P17).

A shape is one call of a public adapter method with invented inputs. `test_gmail_request_conformance.py` runs it on the
REAL adapter with `tests/support/http_boundary_fake.py` as the aiohttp session (the test routes each Gmail URL with the
smallest answer that lets the method run to its end) and checks every request that leaves against
`contracts/providers/gmail/openapi.yaml`. What the requests must look like is the contract, not something written here.

Every kept operation and query parameter of the Gmail entry in `scripts/contracts/extract_subset.py` is reached by a
shape. `NOT_COVERED` names the adapter's URLs the contract does not hold, each with its reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.email import EmailPart
from src.domain.oauth_credential import OAuthCredential

MESSAGE_ID = "msg-1"
ATTACHMENT_ID = "att-1"
FILENAME = "birth-certificate.pdf"
QUERY = "invoice FA-0137 from:clara@example.com"
KEY = "invoice FA-0137"
FROM = datetime(2026, 10, 1)
TO = datetime(2026, 10, 31)

# "<METHOD> <URL as the adapter writes it>" -> why the request has no operation in the Gmail contract. The meta-test
# fails when an entry is covered by an operation now, or no longer called.
NOT_COVERED: dict[str, str] = {
    "POST https://oauth2.googleapis.com/token": (
        "Google OAuth token endpoint — not in the Gmail discovery document; authored contract planned with Google "
        "OAuth (RFC §4a table)"
    ),
}

Call = Callable[[GmailProviderAdapter, OAuthCredential], Awaitable[Any]]


@dataclass(frozen=True)
class Shape:
    id: str
    call: Call


def _part(part_id: str, handle: str) -> EmailPart:
    return EmailPart(part_id=part_id, filename=FILENAME, mime="application/pdf", size=12, handle=handle,
                     displayed_inline=False)


SHAPES: tuple[Shape, ...] = (
    Shape("list_emails/query_and_date_bounds",
          lambda a, c: a.list_emails(c, date_from=FROM, date_to=TO, query=QUERY, max_results=50)),
    Shape("list_emails/exact_instants",
          lambda a, c: a.list_emails(c, date_from=FROM.replace(tzinfo=timezone.utc),
                                     date_to=TO.replace(tzinfo=timezone.utc), exact_instants=True)),
    Shape("list_emails/next_page", lambda a, c: a.list_emails(c, page_token="page-2")),
    Shape("list_emails/no_filter", lambda a, c: a.list_emails(c)),
    Shape("list_headers/key_search", lambda a, c: a.list_headers(c, after=FROM, before=TO, key=KEY, limit=20)),
    Shape("list_headers/window_only", lambda a, c: a.list_headers(c, after=FROM)),
    Shape("batch_get_full_content/one_message", lambda a, c: a.batch_get_full_content(c, [MESSAGE_ID])),
    Shape("list_parts/one_message", lambda a, c: a.list_parts(c, MESSAGE_ID)),
    Shape("get_part/attachment_handle",
          lambda a, c: a.get_part(c, MESSAGE_ID, _part("1", ATTACHMENT_ID), max_bytes=1000)),
    Shape("get_part/inline_part", lambda a, c: a.get_part(c, MESSAGE_ID, _part("2", ""), max_bytes=1000)),
)
