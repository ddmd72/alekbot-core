"""
Email card source — the mail-source helpers of the card writer (email index v2, slice 3). Pure functions, mypy --strict.

What the card core takes as given and the writer has to derive from a fetched mail: the code fields of the mail
(sender name, address and domain, the aware sent time), the owner's zone, whether the owner is a direct recipient
(address comparison with the case, `+tag` and, for the mailbox providers that ignore dots, dot rules), and the
attachment list the card stores together with the one text per attachment the input builder reads.

Public interface:
    GMAIL_DOMAINS                         the domains whose local part ignores dots
    owner_zone(raw) -> str                a valid IANA name, else "UTC"
    mail_fields_of(email_id, from_header, subject, sent_at, zone) -> MailFields
    address_key(address, dotless_domains) -> str
    is_direct_recipient(recipients, owner_addresses) -> bool
    card_attachments_of(attachments) -> (CardAttachment, ...), (text, ...)
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parseaddr
from typing import FrozenSet, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .email_attachment import AttachmentRead as _SliceTwoRead
from .email_attachment import EmailAttachment
from .email_attachment_policy import SHEET_KINDS
from .email_card import CardAttachment, MailFields

GMAIL_DOMAINS: FrozenSet[str] = frozenset({"gmail.com", "googlemail.com"})


def owner_zone(raw: Optional[str]) -> str:
    """The owner's IANA zone when `raw` names one, else "UTC"."""
    name = (raw or "").strip()
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except Exception:
        return "UTC"
    return name


def mail_fields_of(email_id: str, from_header: str, subject: str, sent_at: datetime, zone: str) -> MailFields:
    """The mail's code fields. `from_domain` is the part after the last "@", casefolded ("" when none); a naive
    `sent_at` is UTC (the Gmail adapter returns naive UTC) and becomes timezone-aware."""
    name, address = parseaddr(from_header or "")
    domain = address.rsplit("@", 1)[1].strip().casefold() if "@" in address else ""
    aware = sent_at if sent_at.tzinfo is not None else sent_at.replace(tzinfo=timezone.utc)
    return MailFields(email_id=email_id, from_name=" ".join(name.split()), from_address=address.strip(),
                      from_domain=domain, subject=subject, sent_at=aware, zone=zone)


def address_key(address: str, *, dotless_domains: FrozenSet[str] = GMAIL_DOMAINS) -> str:
    """The comparison key of an address: casefolded, the local part without its `+tag`; for a domain in
    `dotless_domains` the local part also loses its dots and a googlemail.com domain becomes gmail.com. "" for a
    string without "@"."""
    text = address.strip().casefold()
    if "@" not in text:
        return ""
    local, domain = text.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in dotless_domains:
        local = local.replace(".", "")
        if domain in GMAIL_DOMAINS:
            domain = "gmail.com"
    return f"{local}@{domain}"


def is_direct_recipient(recipients: Sequence[str], owner_addresses: Sequence[str]) -> bool:
    """One of the owner's addresses is among the To and Cc addresses."""
    owners = {k for k in (address_key(a) for a in owner_addresses) if k}
    return any(address_key(r) in owners for r in recipients if address_key(r))


def card_attachments_of(attachments: Sequence[EmailAttachment]) -> Tuple[Tuple[CardAttachment, ...], Tuple[str, ...]]:
    """What the card stores per listed attachment and the text the input builder reads for it: the full text of a read
    attachment, the head of a sheet (the drive's rule: header rows first), "" for one not read. Order kept."""
    cards: List[CardAttachment] = []
    texts: List[str] = []
    for a in attachments:
        cards.append(CardAttachment(name=a.name, kind=a.kind, size=a.size, read=a.outcome))
        if a.read is _SliceTwoRead.TEXT:
            texts.append(a.head if a.kind in SHEET_KINDS else (a.text if a.text is not None else a.head))
        else:
            texts.append("")
    return tuple(cards), tuple(texts)
