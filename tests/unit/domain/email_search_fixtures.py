"""Invented mail cards for the email search tests, built with the card core (`build_card_input` + `parse_mail_answer` +
`build_card`, as `email_card_fixtures`). The universe's cast and values only: the owner Alex Doe and the daughter
Jamie Doe; the hardware shop's invoice FA-0137 (15/09/2026, 249,00 EUR); the Albergo Immaginario booking RM-55120;
the Example School parents' meeting (3 March 2026); the Example Mutual Insurance policy MP-2026-118204
(01/04/2026-31/03/2027); the receipts of Supermercado Exemplo Lda. (No. 0931 and No. 0412 printed)."""
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.domain.confirmed_people import ConfirmedPeople
from src.domain.email_card import CardAttachment, EmailCard
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail

RECEIPT_DAYS = (date(2025, 1, 14), date(2025, 2, 11), date(2025, 4, 9), date(2025, 5, 20), date(2025, 7, 3),
                date(2025, 9, 15), date(2025, 11, 6), date(2026, 1, 22), date(2026, 3, 10), date(2026, 6, 4),
                date(2026, 9, 8), date(2026, 9, 22))
SCHOOL_WORDS = ("garden", "library", "swimming", "canteen", "uniform", "calendar", "holiday", "lunch", "trip", "music",
                "sports", "reading", "art", "science", "chess", "parking", "bus", "fees", "photos", "safety", "health",
                "exams", "homework", "clubs", "museum")


def at(day: date, hour: int = 9) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)


def build(subject: str, body: str, sent: date, email_id: str, from_name: str, from_domain: str,
          roster: Optional[ConfirmedPeople] = PEOPLE, attachments: Sequence[CardAttachment] = (),
          texts: Sequence[str] = (), **answer: Any) -> EmailCard:
    """One card: the mail, its body, and the lean answer's fields (the Rome booking's answer is the base)."""
    m = mail(subject, from_name=from_name, from_address=f"info@{from_domain}", from_domain=from_domain,
             sent_at=at(sent), email_id=email_id)
    inp = build_card_input(m, body, list(attachments), list(texts), direct=True, nonce=NONCE)
    return build_card(parse_mail_answer(answer_json(**answer)), m, list(attachments), inp.evidence, roster)


def org(name: str) -> Dict[str, Any]:
    return {"name": name, "type": "organization", "printed": name, "forms": []}


def booking() -> EmailCard:
    return build("Prenotazione confermata RM-55120", BOOKING_BODY, date(2025, 6, 2), "msg_rome01",
                 "Albergo Immaginario", "albergo.example", attachments=(BOOKING_PDF,), texts=("Booking RM-55120",))


def invoice() -> EmailCard:
    body = ("Ferretería El Tornillo Feliz\nFactura FA-0137 de fecha 15/09/2026\nCerradura y dos copias de llave\n"
            "Total 249,00 EUR IVA incluido\n")
    return build("Factura FA-0137", body, date(2026, 9, 15), "msg_invoice01", "Ferretería El Tornillo Feliz",
                 "tornillo.example", language="es", doc_type="invoice", life_domains=["home", "shopping"],
                 title="Factura FA-0137", what_it_is="Invoice for a lock cylinder and two key copies, 249,00 EUR.",
                 dates=[], entities=[org("Ferretería El Tornillo Feliz")],
                 key_values=[{"label": "invoice_number", "printed": "FA-0137", "canonical": "FA0137"},
                             {"label": "amount_total", "printed": "249,00 EUR", "canonical": "249.00"}],
                 topics=["lock replacement"], topics_native=["cerradura"], topic_words=["lock", "key", "hardware"])


def school_notice() -> EmailCard:
    body = ("Dear parents of Jamie,\nThe parents' meeting is on 3 March 2026 at 18:00 in the school hall.\n"
            "Example School\n")
    return build("Reunião de pais, 3 de março", body, date(2026, 3, 2), "msg_school_meeting", "Example School",
                 "school.example", language="en", doc_type="invitation", life_domains=["education", "family"],
                 title="Parents' meeting", what_it_is="Example School invites parents to the meeting of 3 March 2026.",
                 people=[{"printed": "Jamie", "entry_id": "P1"}], about="person",
                 dates=[{"label": "event", "date": "2026-03-03", "date_end": "", "printed": "3 March 2026"}],
                 entities=[org("Example School")], key_values=[], topics=["parents meeting"],
                 topics_native=["reunião de pais"], topic_words=["school", "meeting", "parents"])


def school_news(email_id: str, subject: str, sent: date, word: str = "news") -> EmailCard:
    """A plain Example School mail with no date of its own and no person."""
    body = f"Example School\n{subject}\nThis term: {word}.\n"
    return build(subject, body, sent, email_id, "Example School", "school.example", language="en", doc_type="letter",
                 life_domains=["education"], title=subject, what_it_is=f"Example School writes about {word}.",
                 dates=[], entities=[org("Example School")], key_values=[], topics=[word], topics_native=[],
                 topic_words=["school", word])


def policy() -> EmailCard:
    body = ("Example Mutual Insurance\nApólice MP-2026-118204\nVálida de 01/04/2026 a 31/03/2027\n"
            "Prémio 412,00 EUR\n")
    return build("Apólice MP-2026-118204", body, date(2026, 3, 20), "msg_policy01", "Example Mutual Insurance",
                 "mutual.example", language="pt", doc_type="insurance_policy", life_domains=["vehicle", "finance"],
                 title="Apólice MP-2026-118204", what_it_is="Motor policy valid 01/04/2026 to 31/03/2027, 412,00 EUR.",
                 dates=[{"label": "valid_from", "date": "2026-04-01", "date_end": "", "printed": "01/04/2026"},
                        {"label": "valid_until", "date": "2027-03-31", "date_end": "", "printed": "31/03/2027"}],
                 entities=[org("Example Mutual Insurance")],
                 key_values=[{"label": "policy_number", "printed": "MP-2026-118204", "canonical": "MP2026118204"},
                             {"label": "amount_total", "printed": "412,00 EUR", "canonical": "412.00"}],
                 topics=["motor insurance"], topics_native=["seguro automóvel"], topic_words=["insurance", "car"])


def receipt(day: date, number: str = "", email_id: str = "") -> EmailCard:
    """A receipt of Supermercado Exemplo Lda.: the subject differs from the others only by its date; the printed
    receipt number (when given) is a key value."""
    subject = f"Recibo de compra {day.day:02d}/{day.month:02d}/{day.year}"
    body = f"Supermercado Exemplo Lda.\n{subject}\n" + (f"Recibo No. {number}\n" if number else "") + "Obrigado\n"
    values: List[Dict[str, str]] = ([{"label": "receipt_number", "printed": number, "canonical": number}]
                                    if number else [])
    return build(subject, body, day, email_id or f"msg_receipt_{day.isoformat()}", "Supermercado Exemplo Lda.",
                 "supermercado.example", language="pt", doc_type="receipt", life_domains=["shopping"],
                 title="Recibo de compra", what_it_is="Grocery receipt.", dates=[],
                 entities=[org("Supermercado Exemplo Lda.")], key_values=values, topics=["grocery receipt"],
                 topics_native=["recibo"], topic_words=["supermarket", "groceries"])


def receipts() -> List[EmailCard]:
    """Twelve receipts of one template: No. 0931 and No. 0412 are printed on two of them."""
    printed = {date(2025, 5, 20): "0931", date(2026, 3, 10): "0412"}
    return [receipt(d, printed.get(d, "")) for d in RECEIPT_DAYS]


def everything() -> List[EmailCard]:
    """The whole small world of the service tests."""
    return [booking(), invoice(), school_notice(), school_news("msg_school_feb", "Example School news, February",
                                                               date(2026, 2, 10)),
            school_news("msg_school_apr", "Example School news, April", date(2026, 4, 2)), policy(), *receipts()]
