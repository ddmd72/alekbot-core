"""An in-memory `EmailProviderPort` for the part-level fetch (EXTERNAL_FILES_RFC §4.7 P8a): messages hold listed parts,
a part's bytes are found by the `handle` its listing handed out. It honours the port's contract: EmailMessageGone for
an unknown message, b"" for a part without data, EmailPartTooLarge past `max_bytes`; `fail_with` makes every part
call raise (EmailPartUnavailable for a mailbox that did not answer). The index side (`list_emails`,
`batch_get_full_content`) is empty: a test of those uses its own fake."""
from typing import Dict, List, Optional

from src.domain.email import EmailPart
from src.domain.exceptions import EmailMessageGone, EmailPartTooLarge
from src.ports.email_provider_port import EmailProviderPort


class FakeEmailMailbox(EmailProviderPort):

    def __init__(self, messages: Optional[Dict[str, List[EmailPart]]] = None,
                 blobs: Optional[Dict[str, bytes]] = None) -> None:
        self.messages: Dict[str, List[EmailPart]] = dict(messages or {})
        self.blobs: Dict[str, bytes] = dict(blobs or {})
        self.fail_with: Optional[Exception] = None

    async def list_emails(self, credentials, date_from=None, date_to=None, page_token=None, max_results=100,
                          query=None):
        return [], None

    async def list_headers(self, credentials, *, after=None, before=None, key=None, limit=100):
        return [], False

    async def batch_get_full_content(self, credentials, email_ids, deep=False):
        return {}

    async def refresh_token(self, credentials):
        return credentials

    async def list_parts(self, credentials, message_id):
        self._check(message_id)
        return list(self.messages[message_id])

    async def get_part(self, credentials, message_id, part, max_bytes):
        self._check(message_id)
        data = self.blobs.get(part.handle, b"") if part.handle else b""
        if len(data) > max_bytes:
            raise EmailPartTooLarge(max_bytes)
        return data

    def _check(self, message_id: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if message_id not in self.messages:
            raise EmailMessageGone(message_id)
