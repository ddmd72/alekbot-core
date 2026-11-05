"""`FileConversionService.open` routes an `email:` ref to `open_email` (plan U4 §2 U4a-C): the ref's message and part
ids reach it, its answer comes back as `open`'s (a file, or its note), the prefetch marks its text as email text, and a
ref of the wrong shape is a store error. `open_email` itself is part A3's; here a subclass answers from a table of
invented attachments, so only the routing and the mapping run for real."""
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from src.domain.document_open import EmailOpening, OpenedFile
from src.domain.document_read import CALLER_OPEN, CALLER_PREFETCH, ReadOutcome, ReadStatus
from src.domain.email_open_texts import ATTACHMENT_MISSING_NOTE, EMAIL_OPEN_NOT_CONFIGURED_NOTE
from src.domain.email_ref import format_email_ref
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_access_stub import allow_access

MSG = "msg_school456"
DEADLINE = 1_900_000_000.0
TEXT = "Total due: 680,00 EUR"


class TableOpening(FileConversionService):
    """`open_email` answered from a table keyed by (message id, part id); every call's caller is kept."""

    def __init__(self, table: Dict[Tuple[str, str], EmailOpening]) -> None:
        super().__init__(storage=AsyncMock(spec=FileStoragePort), access=allow_access())
        self.table = table
        self.callers: list = []

    async def open_email(self, user_id, account_id, *, message_id, part_id=None, name=None, consumer=(),
                         deadline_at, caller=CALLER_OPEN):
        self.callers.append(caller)
        return self.table.get((message_id, part_id or ""), EmailOpening(note=ATTACHMENT_MISSING_NOTE))


def _text_opening(part_id: str, name: str, text: str = TEXT) -> EmailOpening:
    body = f"[File: {name}]\n{text}\n[/File: {name}]"
    return EmailOpening(opened=OpenedFile(ReadOutcome(status=ReadStatus.TEXT, kind="pdf", text=text), body=body),
                        ref=format_email_ref(MSG, part_id), name=name, size=2048, mime="application/pdf")


@pytest.fixture
def service() -> TableOpening:
    return TableOpening({(MSG, "1.2"): _text_opening("1.2", "invoice_painting_es.pdf")})


async def test_an_email_ref_opens_the_part_it_names(service):
    opened = await service.open(format_email_ref(MSG, "1.2"), "ann", "ann", deadline_at=DEADLINE)
    assert opened.body == f"[File: invoice_painting_es.pdf]\n{TEXT}\n[/File: invoice_painting_es.pdf]"
    assert service.callers == [CALLER_OPEN]


async def test_a_part_the_email_does_not_hold_comes_back_as_its_note(service):
    opened: Optional[OpenedFile] = await service.open(format_email_ref(MSG, "3"), "ann", "ann", deadline_at=DEADLINE)
    assert opened.note == ATTACHMENT_MISSING_NOTE and opened.outcome is None


@pytest.mark.parametrize("ref", ["email:msg-1/att-1", "email:msg_school456", "email:/1"])
async def test_an_email_ref_of_the_wrong_shape_is_a_store_error(service, ref):
    with pytest.raises(FileNotFoundError, match="not an email attachment ref"):
        await service.open(ref, "ann", "ann", deadline_at=DEADLINE)
    assert service.callers == []


async def test_the_prefetch_marks_the_parts_text_as_email_text(service):
    prefetched = await service.prefetch(format_email_ref(MSG, "1.2"), "ann", "ann", deadline_at=DEADLINE)
    assert "<external_content" in prefetched.content and TEXT in prefetched.content
    assert f'ref="{format_email_ref(MSG, "1.2")}"' in prefetched.content
    assert '="invoice_painting_es.pdf">' in prefetched.content   # the part's name is the wrapper's label
    assert service.callers == [CALLER_PREFETCH]


async def test_the_prefetch_of_a_missing_part_is_its_note(service):
    prefetched = await service.prefetch(format_email_ref(MSG, "3"), "ann", "ann", deadline_at=DEADLINE)
    assert prefetched.note == ATTACHMENT_MISSING_NOTE and prefetched.content == ""


async def test_without_an_email_service_an_email_ref_opens_to_the_not_configured_note():
    """Reviewer (U4a A3): the placeholder refusal ("email tool") is gone now that `open_email` is built; a service
    composed without the email service answers the ref with the not-configured note, nothing opened."""
    plain = FileConversionService(storage=AsyncMock(spec=FileStoragePort), access=allow_access())
    opened = await plain.open(format_email_ref(MSG, "1.2"), "ann", "ann", deadline_at=DEADLINE)
    assert opened.note == EMAIL_OPEN_NOT_CONFIGURED_NOTE and opened.outcome is None
