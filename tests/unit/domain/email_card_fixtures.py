"""Invented mail for the email card tests (the universe's cast and values only): the owner Alex Doe, the daughters
Jamie and Ana Doe, the partner Clara Moreira; the Albergo Immaginario booking RM-55120 (10–14 Aug 2025, 560 EUR), the
Example School parents' meeting (3 March 2026), Dr. Quillen's dental cleaning at Clínica Ribeira (20 Oct 2026), the Example Mutual Insurance policy
MP-2026-118204 and the hardware shop's invoice FA-0137."""
import json
from datetime import datetime, timezone
from typing import Any, Dict

from src.domain.confirmed_people import ConfirmedPeople, parse_confirmed_people
from src.domain.email_card import CardAttachment, MailFields

ZONE = "Europe/Lisbon"
NONCE = "nonce0123456789ab"

PEOPLE: ConfirmedPeople = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [
        {"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"},
        {"name": "Ana Doe", "forms": ["Ana"], "relation": "younger daughter"},
        {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
    ],
})


def mail(subject: str = "Prenotazione confermata RM-55120", *, from_name: str = "Albergo Immaginario",
         from_address: str = "booking@albergo.example", from_domain: str = "albergo.example",
         sent_at: datetime = datetime(2025, 6, 2, 9, 30, tzinfo=timezone.utc), email_id: str = "msg_rome01",
         zone: str = ZONE) -> MailFields:
    return MailFields(email_id=email_id, from_name=from_name, from_address=from_address, from_domain=from_domain,
                      subject=subject, sent_at=sent_at, zone=zone)


BOOKING_BODY = (
    "Gentile Alex Doe,\n"
    "la sua prenotazione RM-55120 è confermata: 10–14 Aug 2025, Via Finta 7, Rome.\n"
    "Totale 560 EUR.\n"
    "Dettagli: https://albergo.example/booking/view?ref=RM-55120&lang=it\n"
)
BOOKING_PDF = CardAttachment(name="RM-55120.pdf", kind="pdf", size=20_000, read="text")


def answer(**overrides: Any) -> Dict[str, Any]:
    """The lean answer of the Rome booking mail, as a dict; fields replaced by `overrides`."""
    data: Dict[str, Any] = {
        "language": "it", "doc_type": "booking", "life_domains": ["travel"], "title": "Prenotazione confermata",
        "what_it_is": "Hotel booking in Rome for 10–14 Aug 2025, 560 EUR.",
        "people": [], "about": "owner",
        "dates": [{"label": "event", "date": "2025-08-10", "date_end": "2025-08-14", "printed": "10–14 Aug 2025"}],
        "entities": [
            {"name": "Rome", "type": "place", "printed": "Rome", "forms": ["Roma"]},
            {"name": "Albergo Immaginario", "type": "organization", "printed": "Albergo Immaginario", "forms": []},
        ],
        "key_values": [
            {"label": "booking_reference", "printed": "RM-55120", "canonical": "RM55120"},
            {"label": "amount_total", "printed": "560 EUR", "canonical": "560.00"},
        ],
        "topics": ["hotel booking"], "topics_native": ["prenotazione"], "topic_words": ["hotel", "trip", "albergo"],
        "substance": "some",
    }
    data.update(overrides)
    return data


def answer_json(**overrides: Any) -> str:
    return json.dumps(answer(**overrides))
