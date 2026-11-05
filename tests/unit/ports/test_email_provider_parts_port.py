"""EmailProviderPort's part-level fetch (EXTERNAL_FILES_RFC §4.7 P8a; plan U4 §1 item 3: `get_part` takes the listed
part, whose opaque handle is valid only with its listing). The contract a fake must honour: a part is fetched by the
part the listing returned, b"" for a part without data, EmailPartTooLarge past `max_bytes`; the shared fake
(`tests/support/fake_email_mailbox.py`) honours it."""
import inspect

import pytest

from src.domain.email import EmailPart
from src.domain.exceptions import EmailMessageGone, EmailPartTooLarge, EmailPartUnavailable
from src.ports.email_provider_port import EmailProviderPort
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.support.oauth_credentials_store import creds

KB = 1024


def test_the_port_declares_the_two_part_methods_abstract():
    for name in ("list_parts", "get_part"):
        method = getattr(EmailProviderPort, name)
        assert getattr(method, "__isabstractmethod__", False)
        assert inspect.iscoroutinefunction(method)


def test_list_parts_takes_the_credential_and_the_message():
    assert list(inspect.signature(EmailProviderPort.list_parts).parameters) == ["self", "credentials", "message_id"]


def test_get_part_takes_the_listed_part_not_a_part_id():
    params = inspect.signature(EmailProviderPort.get_part).parameters
    assert list(params) == ["self", "credentials", "message_id", "part", "max_bytes"]
    assert "EmailPart" in str(params["part"].annotation)


def test_a_provider_without_the_part_methods_cannot_be_built():
    class OldProvider(EmailProviderPort):
        async def list_emails(self, *a, **k): return [], None
        async def batch_get_full_content(self, *a, **k): return {}
        async def refresh_token(self, credentials): return credentials

    with pytest.raises(TypeError):
        OldProvider()  # type: ignore[abstract]


async def test_a_listed_part_is_fetched_by_the_part_its_listing_returned():
    scan = EmailPart(part_id="1.2", filename="scan_0012.pdf", mime="application/pdf", size=2 * KB, handle="att-a")
    inline = EmailPart(part_id="2", filename="logo.png", mime="image/png", size=0, handle="")
    mailbox = FakeEmailMailbox({"msg_school456": [scan, inline]}, {"att-a": b"%PDF-1.7" + b"\0" * (2 * KB - 8)})
    cred = creds("u1", "gmail")
    listed = await mailbox.list_parts(cred, "msg_school456")
    assert [p.part_id for p in listed] == ["1.2", "2"]
    assert (await mailbox.get_part(cred, "msg_school456", listed[0], 4 * KB)).startswith(b"%PDF")
    assert await mailbox.get_part(cred, "msg_school456", listed[1], 4 * KB) == b""
    with pytest.raises(EmailPartTooLarge) as too_large:
        await mailbox.get_part(cred, "msg_school456", listed[0], KB)
    assert too_large.value.limit_bytes == KB
    with pytest.raises(EmailMessageGone):
        await mailbox.list_parts(cred, "msg_gone")
    mailbox.fail_with = EmailPartUnavailable("429")
    with pytest.raises(EmailPartUnavailable):
        await mailbox.get_part(cred, "msg_school456", listed[0], 4 * KB)
