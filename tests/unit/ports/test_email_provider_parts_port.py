"""EmailProviderPort's part-level fetch (EXTERNAL_FILES_RFC §4.7 P8a; plan U4 §1 item 3: `get_part` takes the listed
part, whose opaque handle is valid only with its listing). The contract a fake must honour: a part is fetched by the
part the listing returned, b"" for a part without data, EmailPartTooLarge past `max_bytes`; the shared fake
(`tests/support/fake_email_mailbox.py`) honours it."""

import pytest

from src.domain.email import EmailPart
from src.domain.exceptions import EmailMessageGone, EmailPartTooLarge, EmailPartUnavailable
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.support.oauth_credentials_store import creds

KB = 1024


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
