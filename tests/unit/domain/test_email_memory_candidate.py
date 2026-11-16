"""The email card memory filter (email index v2 §6.7, slice 9): what Stage 3 reads of a carded mail. Only what the mail
prints is sent as printed (unconfirmed names, values and unverified times are dropped), the card's index keys and its
links to the confirmed list never are, title and summary are withheld together when they name a listed person the mail
does not print, and `date` is the sent moment in the owner's zone. Invented mail of the test universe."""
import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.drive_enrichment import DriveProfile, GradedValue, KeyValue, ProfileEntity
from src.domain.email import IndexedEmail
from src.domain.email_card import (
    About, CardAttachment, CardPerson, CardState, EmailCard, MailFields, Substance, TimePoint,
)
from src.domain.email_card_document import card_document
from src.domain.email_memory_candidate import (
    CARD_CANDIDATE_KEYS, EMAIL_CANDIDATES_HEADER, MEMORY_CARD_WAIT, SETTLED_FOR_MEMORY, card_candidate, memory_card,
)
from src.domain.email_memory_mail import MemoryMail
from tests.support.clock_instants import BERLIN, SUMMER_MIDNIGHT
from tests.unit.domain.email_card_fixtures import PEOPLE

ZONE = "Europe/Lisbon"


def booking_card(**over) -> EmailCard:
    """The Albergo Immaginario booking RM-55120 (10-14 Aug 2025, 560 EUR), every kind of value present."""
    profile = DriveProfile(
        title="Prenotazione confermata", what_it_is="Hotel booking in Rome for 10–14 Aug 2025, 560 EUR.",
        language="it", doc_type="booking", life_domains=("travel",),
        entities=(ProfileEntity("Rome", "place", "Rome", ("Roma",), ("Rome",)),
                  ProfileEntity("Albergo Immaginario", "organization", "Albergo Immaginario"),
                  ProfileEntity("Supermercado Exemplo", "organization", "")),
        key_values=(KeyValue("booking_reference", "RM-55120", "RM55120"), KeyValue("amount_total", "560 EUR", "560.00"),
                    KeyValue("phone", "+1 555 0142", "15550142")),
        topics=("hotel booking",), topics_native=("prenotazione",), topic_words=("hotel",), label_words=("booking",),
        domain_words=("travel",), validity_words=(),
        graded=(GradedValue("entity", "Supermercado Exemplo", "not_in_text"), GradedValue("value", "+1 555 0142", "not_in_text"),
                GradedValue("entity", "Rome", "in_text")),
    )
    fields = dict(
        mail=MailFields("msg_rome01", "Albergo Immaginario", "booking@albergo.example", "albergo.example",
                        "Prenotazione confermata RM-55120", datetime(2025, 6, 2, 9, 30, tzinfo=timezone.utc), ZONE),
        attachments=(CardAttachment("RM-55120.pdf", "pdf", 20_000, "text"),
                     CardAttachment("scan.jpg", "image", 9_000, "scan_not_read")),
        profile=profile,
        people=(),
        about=About.OWNER, substance=Substance.SOME,
        times=(TimePoint("event", date(2025, 8, 10), date(2025, 8, 14), "10–14 Aug 2025", True),
               TimePoint("issued", date(2025, 6, 2), date(2025, 6, 2), "2 June", True),
               TimePoint("due", date(2025, 7, 1), date(2025, 7, 1), "", False)),
        state=CardState.CARDED, about_keys=("ab:x",), name_keys=("e:rome",), people_keys=(), month_keys=("m:2025-08",),
        template_key="tk1", card_version=3,
    )
    fields.update(over)
    return EmailCard(**fields)


def with_summary(card: EmailCard, summary: str, title: str = "Notice") -> EmailCard:
    return replace(card, profile=replace(card.profile, what_it_is=summary, title=title))


def school_notice(**over) -> EmailCard:
    """An Example School notice for Jamie's parents' meeting (3 March 2026)."""
    return booking_card(mail=MailFields("msg_school1", "Example School", "office@school.example", "school.example",
                                        "Parents' meeting", datetime(2026, 2, 20, 8, 0, tzinfo=timezone.utc), ZONE),
                        **over)


# ---------------------------------------------------------------------------------------------------------- the keys

def test_a_full_card_sends_exactly_the_candidate_keys_in_order():
    card = booking_card(people=(CardPerson("Clara Moreira", "Clara Moreira", "name"),), about=About.HOUSEHOLD)
    fields = card_candidate(card, ZONE, None, list_unreadable=False).fields
    assert tuple(fields) == CARD_CANDIDATE_KEYS


def test_what_each_key_holds():
    fields = card_candidate(booking_card(), ZONE, None, list_unreadable=False).fields
    assert fields["email_id"] == "msg_rome01" and fields["from"] == "booking@albergo.example"
    assert fields["from_name"] == "Albergo Immaginario" and fields["subject"] == "Prenotazione confermata RM-55120"
    assert fields["doc_type"] == "booking" and fields["life_areas"] == ["travel"] and fields["about"] == "owner"
    assert fields["title"] == "Prenotazione confermata"
    assert fields["summary"] == "Hotel booking in Rome for 10–14 Aug 2025, 560 EUR."
    assert fields["attachments"] == [{"name": "RM-55120.pdf", "read": True}, {"name": "scan.jpg", "read": False}]


def test_unconfirmed_names_and_values_and_unverified_times_are_not_sent():
    fields = card_candidate(booking_card(), ZONE, None, list_unreadable=False).fields
    assert fields["printed_names"] == [{"name": "Rome", "type": "place"},
                                       {"name": "Albergo Immaginario", "type": "organization"}]
    assert fields["printed_values"] == [{"label": "booking_reference", "value": "RM-55120"},
                                        {"label": "amount_total", "value": "560 EUR"}]
    assert fields["times"] == [{"label": "event", "from": "2025-08-10", "to": "2025-08-14"},
                               {"label": "issued", "from": "2025-06-02"}]          # one day: no `to`


def test_a_value_without_its_printed_form_is_sent_by_its_canonical_form():
    card = booking_card()
    card = replace(card, profile=replace(card.profile, key_values=(KeyValue("policy_number", "", "MP2026118204"),)))
    assert card_candidate(card, ZONE, None, list_unreadable=False).fields["printed_values"] == [
        {"label": "policy_number", "value": "MP2026118204"}]


def test_no_link_index_key_word_list_or_lifecycle_field_is_ever_sent():
    card = school_notice(people=(CardPerson("Jamie", "Jamie Doe", "name"),), about=About.PERSON)
    text = json.dumps(card_candidate(card, ZONE, PEOPLE, list_unreadable=False).fields, ensure_ascii=False)
    for absent in ("entry_name", "linked_by", "Jamie Doe", "about_keys", "ab:x", "e:rome", "m:2025-08", "tk1",
                   "template_key", "card_vector", "topics", "prenotazione", "label_words", "language", "substance",
                   "zone", "Lisbon", "albergo.example\"", "kind", "size", "card_version", "card_state", "graded",
                   "Supermercado Exemplo", "+1 555 0142", "text", "category", "tags", "metadata"):
        assert absent not in text, absent
    assert json.loads(text)["people"] == ["Jamie"]


def test_empty_parts_are_left_out_and_the_identity_fields_stay():
    card = booking_card(attachments=(), people=(), times=(),
                        mail=replace(booking_card().mail, from_name=""))
    card = replace(card, profile=replace(card.profile, title="", what_it_is="", life_domains=(), entities=(),
                                         key_values=()))
    fields = card_candidate(card, ZONE, None, list_unreadable=False).fields
    assert set(fields) == {"email_id", "date", "from", "subject", "doc_type", "about"}


def test_date_is_the_sent_moment_in_the_owners_zone_with_its_offset():
    card = booking_card(mail=replace(booking_card().mail, sent_at=SUMMER_MIDNIGHT.utc))
    assert card_candidate(card, BERLIN, None, list_unreadable=False).fields["date"] == "2026-07-15T02:00:00+02:00"
    assert card_candidate(card, "UTC", None, list_unreadable=False).fields["date"] == "2026-07-15T00:00:00+00:00"


# ---------------------------------------------------------------------------------------------------------- withholding

def test_a_listed_person_named_in_the_summary_but_not_printed_withholds_title_and_summary():
    card = with_summary(booking_card(), "Hotel booking in Rome for Clara Moreira, 560 EUR.")
    candidate = card_candidate(card, ZONE, PEOPLE, list_unreadable=False)
    assert candidate.withheld is True
    assert "title" not in candidate.fields and "summary" not in candidate.fields
    assert candidate.fields["printed_values"]                      # the rest of the card is still sent


def test_a_listed_person_named_in_the_title_alone_withholds_both():
    card = with_summary(booking_card(), "Hotel booking in Rome.", title="Booking for Clara")
    assert card_candidate(card, ZONE, PEOPLE, list_unreadable=False).withheld is True


def test_a_person_the_card_links_by_name_keeps_title_and_summary():
    card = with_summary(school_notice(people=(CardPerson("Jamie", "Jamie Doe", "name"),), about=About.PERSON),
                        "Parents' meeting for Jamie on 3 March 2026.")
    candidate = card_candidate(card, ZONE, PEOPLE, list_unreadable=False)
    assert candidate.withheld is False and candidate.fields["summary"] == "Parents' meeting for Jamie on 3 March 2026."


def test_a_person_the_card_links_by_the_printed_words_for_them_keeps_title_and_summary():
    card = with_summary(school_notice(people=(CardPerson("daughter", "Jamie Doe", "relation"),), about=About.PERSON),
                        "Parents' meeting about the owner's daughter on 3 March 2026.")
    assert card_candidate(card, ZONE, PEOPLE, list_unreadable=False).withheld is False


def test_a_listed_person_the_mail_prints_in_the_subject_keeps_title_and_summary():
    card = with_summary(booking_card(mail=replace(booking_card().mail, subject="Reminder for Ana Doe")),
                        "Clínica Ribeira reminder for Ana Doe.")
    assert card_candidate(card, ZONE, PEOPLE, list_unreadable=False).withheld is False


def test_the_owner_named_in_the_summary_withholds_nothing():
    single = parse_confirmed_people({"user": {"name": "Alex Doe", "forms": ["Alex"]},
                                     "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"}]})
    card = with_summary(booking_card(), "Hotel booking in Rome for Alex.")
    assert card_candidate(card, ZONE, single, list_unreadable=False).withheld is False


def test_no_list_keeps_title_and_summary():
    card = with_summary(booking_card(), "Hotel booking in Rome for Clara Moreira, 560 EUR.")
    candidate = card_candidate(card, ZONE, None, list_unreadable=False)
    assert candidate.withheld is False and "Clara Moreira" in candidate.fields["summary"]


def test_an_unreadable_list_withholds_title_and_summary_on_every_card():
    candidate = card_candidate(booking_card(), ZONE, None, list_unreadable=True)
    assert candidate.withheld is True and "title" not in candidate.fields and "summary" not in candidate.fields


# ---------------------------------------------------------------------------------------------------------- memory_card

def v1(email_id: str = "msg_rome01") -> IndexedEmail:
    when = datetime(2025, 6, 2, 9, 30)
    return IndexedEmail(email_id=email_id, user_id="u", account_id="a", source="gmail", text="Hotel booked.",
                        tags=["hotel"], category="travel", metadata={}, subject="s", from_address="f@example.com",
                        email_date=when, indexed_at=when)


def test_a_carded_readable_document_reads_back_as_its_card():
    card = booking_card()
    assert memory_card(MemoryMail(v1(), "carded", card_document(card))) == card


@pytest.mark.parametrize("state", ["empty", "abandoned", "source_gone", "pending", ""])
def test_every_other_state_has_no_card(state):
    assert memory_card(MemoryMail(v1(), state, card_document(booking_card()) if state else None)) is None


def test_a_carded_mail_without_its_fields_or_with_an_unreadable_card_has_none():
    broken = card_document(booking_card())
    del broken["card"]["profile"]
    assert memory_card(MemoryMail(v1(), "carded", broken)) is None
    assert memory_card(MemoryMail(v1(), "carded", None)) is None


# ---------------------------------------------------------------------------------------------------------- constants

def test_the_settled_states_wait_and_header():
    assert SETTLED_FOR_MEMORY == ("carded", "empty", "abandoned", "source_gone")
    assert MEMORY_CARD_WAIT.total_seconds() == 6 * 3600
    assert "[" not in EMAIL_CANDIDATES_HEADER and "system" not in EMAIL_CANDIDATES_HEADER.lower()
