"""
Email card text — the card's embedding text and the result line Smart reads (email index v2 §2.5; card slice 1).
Pure: no I/O.

**One vector** per card, over: the doc type's names · the sender's name and domain · the subject · the title · the
summary · the entities the mail prints · the English topics. Not the raw body (storing the body by another name), not
attachment names or tags (one signal voted several times).

**The result line** (code-built; the search puts it inside the untrusted wrapper), at most RESULT_LINE_CHARS_MAX:
    • [id] 2025-06-02 · booking · Albergo Immaginario (albergo.example) · "Prenotazione confermata RM-55120"
      → Hotel booking in Rome for 10–14 Aug 2025, 560 EUR. | when: event 10–14 Aug 2025 | 📎 1 read
      matched: booking reference "RM-55120" (printed); place "Rome" (printed)
The matched line names what found the card and how far to trust it: `printed` (the mail prints it), `by translation`
(a spelling or script the mail does not print), `automatic` (model-written). To fit, the matched list loses its last
items first, then the summary and the subject are shortened.

Public interface:
    card_embedding_text(card) -> str
    render_result_line(card, matched_keys=()) -> str
    READING_WORDS: SourceClass value -> reading word
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

from .drive_key_sources import AboutField, AboutSource
from .drive_keys import about_sources
from .drive_match import SourceClass
from .drive_enrichment import GradedField, is_unconfirmed
from .email_card import EmailCard
from .email_card_keys import card_record
from .email_card_labels import card_type_names
from .email_card_params import RESULT_LINE_CHARS_MAX
from .email_card_time import render_when, when_of

READING_WORDS: Mapping[str, str] = {
    SourceClass.VERIFIED_RECORD.value: "printed",
    SourceClass.PASSAGE_WORDS.value: "printed",
    SourceClass.TRANSLATION.value: "by translation",
    SourceClass.AUTOMATIC_RECORD.value: "automatic",
}
_ELLIPSIS = "…"
_SUMMARY_MIN = 60
_SUBJECT_MIN = 30
_FIELD_NAMES: Mapping[AboutField, str] = {
    AboutField.DATE: "date", AboutField.EFFECTIVE_DATE: "sent", AboutField.LABEL_WORD: "type word",
    AboutField.TOPIC_WORD: "topic word", AboutField.DOMAIN_WORD: "life area word",
    AboutField.VALIDITY_WORD: "validity word",
}


def card_embedding_text(card: EmailCard) -> str:
    """The text of the card's one vector (module docstring), its parts joined by " · ", empty parts left out."""
    profile = card.profile
    printed = [e.printed or e.name for e in profile.entities
               if not is_unconfirmed(profile, GradedField.ENTITY, e.printed.strip() or e.name)]
    sender = " ".join(p for p in (card.mail.from_name, card.mail.from_domain) if p)
    parts = (", ".join(card_type_names(profile.doc_type)), sender, card.mail.subject, profile.title,
             profile.what_it_is, ", ".join(printed), ", ".join(profile.topics))
    return " · ".join(p for p in parts if p.strip())


def render_result_line(card: EmailCard, matched_keys: Sequence[str] = ()) -> str:
    """The card's result lines for Smart (module docstring)."""
    mail, profile = card.mail, card.profile
    sender = mail.from_name or mail.from_address
    if mail.from_domain:
        sender = f"{sender} ({mail.from_domain})"
    head = f"• [{mail.email_id}] {mail.sent_day.isoformat()} · {profile.doc_type.replace('_', ' ')} · {sender} · "
    tail = [f"when: {render_when(when_of(card.times, mail.sent_day))}"]
    files = _files(card)
    if files:
        tail.append(files)
    matched = _matched(card, matched_keys)
    subject, summary = mail.subject, profile.what_it_is or profile.title

    def lines() -> str:
        out = f'{head}"{subject}"\n  → {" | ".join([summary, *tail]) if summary else " | ".join(tail)}'
        if matched:
            out += "\n  matched: " + "; ".join(matched)
        return out

    while len(lines()) > RESULT_LINE_CHARS_MAX and len(matched) > 1:
        matched = matched[:-1]
    over = len(lines()) - RESULT_LINE_CHARS_MAX
    if over > 0 and len(summary) > _SUMMARY_MIN:
        summary = _cut(summary, max(_SUMMARY_MIN, len(summary) - over))
    over = len(lines()) - RESULT_LINE_CHARS_MAX
    if over > 0 and len(subject) > _SUBJECT_MIN:
        subject = _cut(subject, max(_SUBJECT_MIN, len(subject) - over))
    over = len(lines()) - RESULT_LINE_CHARS_MAX
    if over > 0 and matched:
        matched = [_cut(matched[0], max(1, len(matched[0]) - over))]
    return lines()


def _files(card: EmailCard) -> str:
    listed = len(card.attachments)
    if not listed:
        return ""
    read = sum(1 for a in card.attachments if a.read_ok)
    return f"📎 {read} read" if read == listed else f"📎 {read} of {listed} read"


def _matched(card: EmailCard, keys: Sequence[str]) -> List[str]:
    """One phrase per matched source, in key order, each once: `booking reference "RM-55120" (printed)`."""
    if not keys:
        return []
    sources = about_sources(card_record(card))
    out: Dict[str, None] = {}
    for key in keys:
        for source in sources.get(key, ()):
            out.setdefault(_phrase(source), None)
    return list(out)


def _phrase(source: AboutSource) -> str:
    if source.field is AboutField.VALUE:
        what = source.detail.replace("_", " ")
    elif source.field in (AboutField.ENTITY, AboutField.ENTITY_FORM):
        what = source.detail or "name"
    else:
        what = _FIELD_NAMES.get(source.field, source.field.value.replace("_", " "))
    reading = READING_WORDS.get(source.source_class.value, "automatic") if source.source_class else "automatic"
    return f'{what} "{source.printed}" ({reading})'


def _cut(text: str, limit: int) -> str:
    """The text in `limit` characters, an ellipsis last (called only for a text longer than `limit`)."""
    return text[:max(0, limit - 1)].rstrip() + _ELLIPSIS
