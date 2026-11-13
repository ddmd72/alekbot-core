"""
Email card build — the graded, keyed card from the model's answer and the mail as built (email index v2 §2, §6.3
"parse → grade → time envelope, keys, template key, card text"; card slice 1). Pure: no I/O, no clock.

Steps, all over the grading evidence the input builder produced (`email_card_input.CardEvidence`):
1. the time envelope (`email_card_time`): the issued == sent day dropped, ranges, yearless dates, weekdays;
2. the drive's grading (`drive_profile.grade`) of entities, key values, dates and prose numbers, unchanged; the dates'
   grades are then the envelope's own (a verified point is held, an unverified one `not_in_text`), since the drive's
   date grade reads neither spans nor yearless forms;
3. the card's own doc type over the drive grader's `other` (a mail-only type), and the mail-only type's names as label
   words (`lw:`), which the drive keyer reads only from its own table for drive types;
4. people and `about` (`email_card_people`), then every key family.

**Empty** (§4): the model's `substance: none`, or nothing printed (no entity, key value or date held by the mail) on a
`newsletter` or `other` type. An empty card is stored and hidden from v2 search; a later CARD_VERSION re-reads it.

Public interface:
    build_card(answer, mail, attachments, evidence, people) -> EmailCard
    card_state_of(answer_substance, profile) -> CardState            carded | empty
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional, Sequence, Tuple

from .confirmed_people import ConfirmedPeople
from .drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, is_unconfirmed
from .drive_grade import TextEvidence
from .drive_keys import about_keys_of
from .drive_profile import grade
from .email_card import CardAttachment, CardState, EmailCard, MailAnswer, MailFields, Substance, TimePoint
from .email_card_input import CardEvidence
from .email_card_keys import card_name_keys, keyed_record, template_key
from .email_card_labels import MAIL_DOC_TYPE_NAMES
from .email_card_params import CARD_VERSION
from .email_card_people import people_keys, resolve_people
from .email_card_time import month_keys, profile_dates, time_envelope

_EMPTY_WHEN_UNPRINTED = frozenset({"newsletter", "other"})


@dataclass(frozen=True)
class _Chunk:
    text: str


def build_card(answer: MailAnswer, mail: MailFields, attachments: Sequence[CardAttachment], evidence: CardEvidence,
               people: Optional[ConfirmedPeople]) -> EmailCard:
    """The card (module docstring)."""
    text = TextEvidence(list(evidence.chunks), evidence.name)
    sent_day = mail.sent_day
    times = time_envelope(answer.dates, sent_day, text)
    asked = DriveProfile(
        title=answer.title, what_it_is=answer.what_it_is, language=answer.language, doc_type=answer.doc_type,
        life_domains=answer.life_domains, dates=profile_dates(times), entities=answer.entities,
        key_values=answer.key_values, topics=answer.topics, topics_native=answer.topics_native,
        topic_words=answer.topic_words)
    graded, _ = grade(asked, [_Chunk(c) for c in evidence.chunks], evidence.name)
    profile = replace(graded, doc_type=answer.doc_type, label_words=MAIL_DOC_TYPE_NAMES.get(answer.doc_type, ()),
                      graded=_with_envelope_grades(graded.graded, times))
    persons, about = resolve_people(answer.people, answer.about, people, text)
    return EmailCard(
        mail=mail, attachments=tuple(attachments), profile=profile, people=persons, about=about,
        substance=answer.substance, times=times, state=card_state_of(answer.substance, profile),
        about_keys=about_keys_of(keyed_record(profile, sent_day)),
        name_keys=card_name_keys(mail.subject, mail.from_name),
        people_keys=people_keys(persons), month_keys=month_keys(times, sent_day),
        template_key=template_key(mail.from_domain, mail.subject), card_version=CARD_VERSION)


def card_state_of(substance: Substance, profile: DriveProfile) -> CardState:
    """`empty` for `substance: none`, or a `newsletter`/`other` card the mail prints nothing of; else `carded`."""
    if substance is Substance.NONE:
        return CardState.EMPTY
    if profile.doc_type in _EMPTY_WHEN_UNPRINTED and not _prints_something(profile):
        return CardState.EMPTY
    return CardState.CARDED


def _prints_something(profile: DriveProfile) -> bool:
    held = [(GradedField.ENTITY, e.printed or e.name) for e in profile.entities]
    held += [(GradedField.VALUE, v.printed or v.canonical) for v in profile.key_values]
    held += [(GradedField.DATE, d.iso.isoformat()) for d in profile.dates]
    return any(not is_unconfirmed(profile, gfield, value) for gfield, value in held)


def _with_envelope_grades(grades: Tuple[GradedValue, ...], times: Sequence[TimePoint]) -> Tuple[GradedValue, ...]:
    """The drive's grades with every date's replaced by the envelope's verdict: held when a verified point has the
    day (the drive's held grade kept, else `in_text_as` with the printed form), `not_in_text` otherwise."""
    drive_dates: Dict[str, GradedValue] = {g.value: g for g in grades if g.field == GradedField.DATE.value}
    verdicts: Dict[str, GradedValue] = {}
    for p in times:
        for day in dict.fromkeys((p.start, p.end)):
            iso = day.isoformat()
            if p.verified:
                known = drive_dates.get(iso)
                if known is not None and known.grade != Grade.NOT_IN_TEXT.value:
                    verdicts[iso] = known
                else:
                    verdicts[iso] = GradedValue(field=GradedField.DATE.value, value=iso, grade=Grade.IN_TEXT_AS.value,
                                                seen_as=p.printed)
            elif iso not in verdicts:
                verdicts[iso] = GradedValue(field=GradedField.DATE.value, value=iso,
                                            grade=Grade.NOT_IN_TEXT.value)
    others = tuple(g for g in grades if g.field != GradedField.DATE.value)
    return others + tuple(verdicts.values())
