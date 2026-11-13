"""
Email card document — the stored shape of an `EmailCard` and its read-back (email index v2, slice 3). Pure, mypy --strict.

Top-level fields are the queried ones (state, version, the key lists); everything else is one `card` map. Values are
JSON-native only (str, int, bool, None, list, dict): dates are ISO strings and tuples are lists, so the document passes
through Firestore, JSON files and the test fake unchanged. Bodies and document text are never part of a card, so they are
never part of the document. `card_from_document` is the one reader (the store of slice 6, the re-card file of slice 4);
a missing list reads as empty, a missing required field is a ValueError.

Not named `card_record`: that name is the keyer's (`email_card_keys`).

Public interface:
    card_document(card) -> dict
    card_from_document(doc) -> EmailCard          ValueError on a missing or malformed required field
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Tuple

from .drive_enrichment import DriveProfile, GradedValue, KeyValue, ProfileDate, ProfileEntity
from .email_card import (
    About, CardAttachment, CardPerson, CardState, EmailCard, MailFields, Substance, TimePoint,
)



def card_document(card: EmailCard) -> Dict[str, Any]:
    """The card as a JSON-native document."""
    mail, profile = card.mail, card.profile
    return {
        "card_state": card.state.value,
        "card_version": card.card_version,
        "about_keys": list(card.about_keys),
        "name_keys": list(card.name_keys),
        "people_keys": list(card.people_keys),
        "month_keys": list(card.month_keys),
        "template_key": card.template_key,
        "card": {
            "mail": {
                "email_id": mail.email_id, "from_name": mail.from_name, "from_address": mail.from_address,
                "from_domain": mail.from_domain, "subject": mail.subject, "sent_at": mail.sent_at.isoformat(),
                "zone": mail.zone,
            },
            "attachments": [{"name": a.name, "kind": a.kind, "size": a.size, "read": a.read}
                            for a in card.attachments],
            "profile": {
                "title": profile.title, "what_it_is": profile.what_it_is, "language": profile.language,
                "doc_type": profile.doc_type, "life_domains": list(profile.life_domains),
                "dates": [{"label": d.label, "iso": d.iso.isoformat(), "printed": d.printed} for d in profile.dates],
                "entities": [{"name": e.name, "type": e.type, "printed": e.printed, "forms": list(e.forms),
                              "found": list(e.found)} for e in profile.entities],
                "key_values": [{"label": v.label, "printed": v.printed, "canonical": v.canonical}
                               for v in profile.key_values],
                "topics": list(profile.topics), "topics_native": list(profile.topics_native),
                "topic_words": list(profile.topic_words), "label_words": list(profile.label_words),
                "domain_words": list(profile.domain_words), "validity_words": list(profile.validity_words),
                "graded": [{"field": g.field, "value": g.value, "grade": g.grade, "seen_as": g.seen_as}
                           for g in profile.graded],
            },
            "people": [{"printed": p.printed, "entry_name": p.entry_name, "linked_by": p.linked_by}
                       for p in card.people],
            "about": card.about.value,
            "substance": card.substance.value,
            "times": [{"label": t.label, "start": t.start.isoformat(), "end": t.end.isoformat(),
                       "printed": t.printed, "verified": t.verified} for t in card.times],
        },
    }


def card_from_document(doc: Mapping[str, Any]) -> EmailCard:
    """The card of a document `card_document` wrote. ValueError when a required field is missing or malformed."""
    try:
        return _read(doc)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"card document unreadable: {type(exc).__name__}: {exc}") from exc


def _read(doc: Mapping[str, Any]) -> EmailCard:
    body = doc["card"]
    m = body["mail"]
    p = body["profile"]
    mail = MailFields(
        email_id=m["email_id"], from_name=m["from_name"], from_address=m["from_address"],
        from_domain=m["from_domain"], subject=m["subject"], sent_at=datetime.fromisoformat(m["sent_at"]),
        zone=m["zone"])
    profile = DriveProfile(
        title=p["title"], what_it_is=p["what_it_is"], language=p["language"], doc_type=p["doc_type"],
        life_domains=_strs(p, "life_domains"),
        dates=tuple(ProfileDate(label=d["label"], iso=date.fromisoformat(d["iso"]), printed=d.get("printed", ""))
                    for d in _items(p, "dates")),
        entities=tuple(ProfileEntity(name=e["name"], type=e["type"], printed=e.get("printed", ""),
                                     forms=_strs(e, "forms"), found=_strs(e, "found")) for e in _items(p, "entities")),
        key_values=tuple(KeyValue(label=v["label"], printed=v.get("printed", ""), canonical=v.get("canonical", ""))
                         for v in _items(p, "key_values")),
        topics=_strs(p, "topics"), topics_native=_strs(p, "topics_native"), topic_words=_strs(p, "topic_words"),
        label_words=_strs(p, "label_words"), domain_words=_strs(p, "domain_words"),
        validity_words=_strs(p, "validity_words"),
        graded=tuple(GradedValue(field=g["field"], value=g["value"], grade=g["grade"], seen_as=g.get("seen_as", ""))
                     for g in _items(p, "graded")))
    return EmailCard(
        mail=mail,
        attachments=tuple(CardAttachment(name=a["name"], kind=a.get("kind", ""), size=int(a.get("size", 0)),
                                         read=a["read"]) for a in _items(body, "attachments")),
        profile=profile,
        people=tuple(CardPerson(printed=x["printed"], entry_name=x.get("entry_name", ""),
                                linked_by=x.get("linked_by", "")) for x in _items(body, "people")),
        about=About(body["about"]), substance=Substance(body["substance"]),
        times=tuple(TimePoint(label=t["label"], start=date.fromisoformat(t["start"]),
                              end=date.fromisoformat(t["end"]), printed=t.get("printed", ""),
                              verified=bool(t["verified"])) for t in _items(body, "times")),
        state=CardState(doc["card_state"]),
        about_keys=_strs(doc, "about_keys"), name_keys=_strs(doc, "name_keys"), people_keys=_strs(doc, "people_keys"),
        month_keys=_strs(doc, "month_keys"), template_key=doc["template_key"], card_version=int(doc["card_version"]))


def _strs(obj: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    return tuple(obj.get(key) or ())


def _items(obj: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    return list(obj.get(key) or ())
