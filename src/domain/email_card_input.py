"""
Email card input — the extraction input of one mail and its grading evidence (email index v2 §3, §2.2; card slice 1).
Pure: the caller supplies the already-fetched body, the already-read attachment texts and the wrapper's nonce.

The body is the adapter's cleaned text, built for the model in three steps:
1. links become their host (`Read more (track.example)`): link-heavy bodies spend most characters on URLs; a key
   printed only inside a URL is then not printed;
2. quoted history is shortened, not removed: the first QUOTE_KEEP_CHARS of the quoted block stay (a reply "yes,
   confirmed" needs what it answers), then a gap marker;
3. the body is cut to head BODY_HEAD_CHARS + tail BODY_TAIL_CHARS at a line or sentence end, a gap marker between.
Attachments are listed with their read outcome; at most ATTACHMENTS_READ_MAX read texts follow, each cut to head
ATTACHMENT_HEAD_CHARS + tail ATTACHMENT_TAIL_CHARS and then to its share of INPUT_CHARS_MAX (never below
ATTACHMENT_MIN_CHARS). The mail cannot close its own structure (`<mail`, `<body`, `<attachment` lose their `<`), and
the whole block goes inside the repo's untrusted wrapper (`wrap_untrusted`, store `email`), the neutral reminder first.

The grading evidence (§2.2) is the same pieces without any gap marker: the subject, the sender's display name, the
body as built (the kept quoted head included), each included attachment text as built; the name line holds every
listed attachment's name, one per line (a number printed only in a file name is printed in the mail). Code never puts
filler into the evidence.

`CardInput` and `CardEvidence` are one value shape (the builder's output and its evidence part) and share this module
on purpose.

Public interface:
    CardEvidence(chunks, name)
    CardInput(text, body, evidence, body_cut, quoted_cut, attachments_included)
    build_card_input(mail, body, attachments, texts, *, direct, nonce) -> CardInput
    grade_evidence(subject, from_name, body_pieces, attachment_pieces, attachment_names) -> CardEvidence
    links_to_host(text), shorten_quoted(text) -> (head, cut), head_tail(text, head, tail) -> (pieces, cut)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from .email_card import AttachmentRead, CardAttachment, MailFields
from .email_card_params import (
    ATTACHMENT_HEAD_CHARS, ATTACHMENT_MIN_CHARS, ATTACHMENT_TAIL_CHARS, ATTACHMENTS_READ_MAX, BODY_HEAD_CHARS,
    BODY_TAIL_CHARS, INPUT_CHARS_MAX, QUOTE_KEEP_CHARS,
)
from .file_ref import FileRefKind
from .untrusted_text import EXTERNAL_REMINDER, wrap_untrusted

BODY_GAP = "[… the middle of the mail is left out …]"
QUOTE_GAP = "[… earlier quoted mail shortened …]"
FILE_GAP = "[… the middle of the file is left out …]"
FIT_GAP = "[… cut to fit the input limit …]"

_URL = re.compile(r"https?://([^\s/<>)\]\"']+)[^\s<>)\]\"']*", re.IGNORECASE)
# The start of a quoted earlier mail: a reply header in the product and universe languages, an "Original Message"
# rule, a forwarded header block, or a line quoted with ">".
_QUOTE_MARK = re.compile(
    r"(?:On .{5,150}? wrote:|El .{5,150}? escribi[oó]:|Em .{5,150}? escreveu:|Le .{5,150}? a [ée]crit\s?:|"
    r"Am .{5,150}? schrieb .{0,80}?:|-{2,}\s*(?:Original Message|Mensaje original|Mensagem original|"
    r"Ursprüngliche Nachricht)\s*-{2,}|\bFrom:\s.{0,200}?\b(?:Sent|Date):\s|\bDe:\s.{0,200}?\b(?:Enviado|Date):\s|"
    r"\bVon:\s.{0,200}?\bGesendet:\s|(?:\d{1,2}[./]\d{1,2}[./]\d{2,4}).{0,120}(?:пише|писав|написала?):|"
    r"(?m:^>\s))", re.DOTALL)
_QUOTE_MIN_START = 20      # a mark at the very start is a forward or a bare quote: nothing of the mail's own before it
_BOUNDARY = re.compile(r"\n|(?<=[.!?。…])\s")
_TEMPLATE_TAGS = re.compile(r"<(/?)(mail|body|attachment)", re.IGNORECASE)
_OUTCOMES = {
    AttachmentRead.SCAN_NOT_READ.value: "not read: scanned pages",
    AttachmentRead.LOCKED.value: "not read: password-protected",
}


@dataclass(frozen=True)
class CardEvidence:
    """What `drive_profile.grade` grades the card against: chunk texts and the name line."""
    chunks: Tuple[str, ...]
    name: str


@dataclass(frozen=True)
class CardInput:
    """The mail block for the extraction call (`text`: reminder + wrapped block), the body as built (with its gap
    markers), the grading evidence, and what was cut."""
    text: str
    body: str
    evidence: CardEvidence
    body_cut: bool
    quoted_cut: bool
    attachments_included: int


def links_to_host(text: str) -> str:
    """Every http(s) link becomes `(host)`."""
    return _URL.sub(lambda m: f"({m.group(1).lower()})", text)


def shorten_quoted(text: str) -> Tuple[str, bool]:
    """The text up to the quoted earlier mail plus the quoted block's first QUOTE_KEEP_CHARS (cut at a line or
    sentence end), and whether anything was left out. The gap marker is the caller's."""
    m = _QUOTE_MARK.search(text)
    if m is None or m.start() < _QUOTE_MIN_START:
        return text, False
    quoted = text[m.start():]
    if len(quoted) <= QUOTE_KEEP_CHARS:
        return text, False
    return text[:m.start()] + _cut_head(quoted, QUOTE_KEEP_CHARS), True


def head_tail(text: str, head: int, tail: int) -> Tuple[Tuple[str, ...], bool]:
    """The text whole (one piece), or its head and tail cut at a line or sentence end (two pieces)."""
    if len(text) <= head + tail:
        return (text,), False
    return (_cut_head(text, head), _cut_tail(text, tail)), True


def grade_evidence(subject: str, from_name: str, body_pieces: Sequence[str], attachment_pieces: Sequence[str],
                   attachment_names: Sequence[str]) -> CardEvidence:
    """The grading evidence (module docstring): empty pieces are left out, never replaced by filler."""
    chunks = tuple(c for c in (subject, from_name, *body_pieces, *attachment_pieces) if c.strip())
    return CardEvidence(chunks=chunks, name="\n".join(n for n in attachment_names if n.strip()))


def build_card_input(mail: MailFields, body: str, attachments: Sequence[CardAttachment], texts: Sequence[str], *,
                     direct: bool, nonce: str) -> CardInput:
    """The extraction input of one mail (module docstring). `texts[i]` is the read text of `attachments[i]` ("" when
    not read). `direct`: one of the owner's addresses is in To or Cc."""
    if len(texts) != len(attachments):
        raise ValueError("one text per listed attachment")
    linked = links_to_host(body)
    kept, quoted_cut = shorten_quoted(linked)
    pieces, body_cut = head_tail(kept, BODY_HEAD_CHARS, BODY_TAIL_CHARS)
    built_body = f"\n{BODY_GAP}\n".join(pieces) + (f" {QUOTE_GAP}" if quoted_cut else "")

    readable = [i for i, a in enumerate(attachments) if a.read_ok and texts[i].strip()]
    included = readable[:ATTACHMENTS_READ_MAX]
    listing = _attachment_list(attachments, set(included), set(readable))
    sender = f"{mail.from_name} <{mail.from_address}>" if mail.from_name else mail.from_address
    sent = mail.sent_at.astimezone(ZoneInfo(mail.zone)).strftime("%Y-%m-%d %H:%M")
    head = (f"<mail>\nFrom: {_neutral(sender)}\nSubject: {_neutral(mail.subject)}\nSent: {sent} ({mail.zone})\n"
            f"Owner is a direct recipient: {'yes' if direct else 'no'}\nAttachments:{listing}\n"
            f"<body>\n{_neutral(built_body)}\n</body>")
    budget = INPUT_CHARS_MAX - len(head) - 40 * len(included)
    share = max(ATTACHMENT_MIN_CHARS, budget // len(included)) if included else 0
    blocks: List[str] = []
    attachment_pieces: List[str] = []
    for i in included:
        cut, _ = head_tail(texts[i], ATTACHMENT_HEAD_CHARS, ATTACHMENT_TAIL_CHARS)
        parts = _fit(cut, share)
        attachment_pieces.extend(parts)
        shown = f"\n{FILE_GAP}\n".join(parts) + (f"\n{FIT_GAP}" if parts != cut else "")
        name = _neutral(attachments[i].name).replace('"', "'")
        blocks.append(f'<attachment name="{name}">\n{_neutral(shown)}\n</attachment>')
    block = "\n".join([head, *blocks, "</mail>"])
    wrapped = wrap_untrusted(block, nonce=nonce, store=FileRefKind.EMAIL.value, ref=f"email:{mail.email_id}",
                             label="mail")
    evidence = grade_evidence(mail.subject, mail.from_name, pieces, attachment_pieces,
                              [a.name for a in attachments])
    return CardInput(text=f"{EXTERNAL_REMINDER}\n\n{wrapped}", body=built_body, evidence=evidence,
                     body_cut=body_cut, quoted_cut=quoted_cut, attachments_included=len(included))


# -- implementation -------------------------------------------------------------------------------------------------

def _cut_head(text: str, n: int) -> str:
    """At most n characters from the start, ending at the last line or sentence end in the window's last fifth."""
    window = text[:n]
    ends = [m.end() for m in _BOUNDARY.finditer(window, int(n * 0.8))]
    return (window[:ends[-1]] if ends else window).rstrip()


def _cut_tail(text: str, n: int) -> str:
    """At most n characters from the end, starting after the first line or sentence end in its first quarter."""
    start = len(text) - n
    m = _BOUNDARY.search(text, start, start + n // 4)
    return (text[m.end():] if m else text[start:]).lstrip()


def _fit(parts: Tuple[str, ...], share: int) -> Tuple[str, ...]:
    """The pieces within `share` characters: the head is kept first, the tail gets what remains."""
    out: List[str] = []
    left = share
    for part in parts:
        if len(part) <= left:
            out.append(part)
            left -= len(part)
            continue
        out.append(_cut_head(part, left))     # the head is cut: the tail is left out
        break
    return tuple(p for p in out if p)


def _attachment_list(attachments: Sequence[CardAttachment], included: Set[int], readable: Set[int]) -> str:
    if not attachments:
        return " none"
    lines = []
    for i, a in enumerate(attachments):
        lines.append(f"\n- {_neutral(a.name)} ({a.kind or 'unknown kind'}, {a.size} bytes): "
                     f"{_outcome(a, i in included, i in readable)}")
    return "".join(lines)


def _outcome(a: CardAttachment, included: bool, readable: bool) -> str:
    if included:
        return "read, text below"
    if readable:
        return "read, text not included: over the per-mail limit"
    if a.read_ok:
        return "read, no text"
    if a.read in _OUTCOMES:
        return _OUTCOMES[a.read]
    if a.read.startswith("skipped:"):
        return f"not read: {a.read.split(':', 1)[1].replace('_', ' ')}"
    return f"not read: {a.read.replace('_', ' ')}"


def _neutral(text: str) -> str:
    """A mail cannot close its own block: the template's tag openers lose their `<`."""
    return _TEMPLATE_TAGS.sub(lambda m: "‹" + m.group(1) + m.group(2), text)
