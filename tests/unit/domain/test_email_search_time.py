"""Email search time (email index v2 §5.3.3; search slice 4): the window a request names, the one the slots give, the
months it touches and who is in it. Invented cast only."""
from datetime import date

from src.domain.email_search_query import MailQuerySlots, TimeSlot, mail_concepts
from src.domain.email_search_time import (
    TimeWindow, choose_window, in_window, named_window, text_names_time, window_months,
)
from tests.unit.domain.email_search_fixtures import booking, build, invoice, policy

TODAY = date(2026, 11, 13)


def _named(text, today=TODAY):
    return named_window(mail_concepts(text).time, today)


def _slots(time):
    return MailQuerySlots(subject="", doc_types=(), domains=(), people=(), time=time, exact_keys=(), scope="top")


def test_a_bare_month_is_the_latest_one_that_has_started():
    sept = _named("mail from September")
    assert (sept.start, sept.end, sept.label, sept.filters, sept.intent) == (
        date(2026, 9, 1), date(2026, 9, 30), "September 2026", True, "period")
    dec = _named("mail from December")
    assert (dec.start, dec.end, dec.label) == (date(2025, 12, 1), date(2025, 12, 31), "December 2025")
    assert _named("mail from November").start == date(2026, 11, 1)


def test_a_month_with_a_year_a_year_and_a_date():
    assert _named("September 2026").start == date(2026, 9, 1)
    year = _named("2025")
    assert (year.start, year.end, year.label) == (date(2025, 1, 1), date(2025, 12, 31), "2025")
    day = _named("14.03.2025")
    assert (day.start, day.end, day.label) == (date(2025, 3, 14), date(2025, 3, 14), "14 Mar 2025")


def test_two_months_make_one_span():
    window = _named("March to May")
    assert (window.start, window.end, window.label) == (date(2026, 3, 1), date(2026, 5, 31), "1 Mar – 31 May 2026")


def test_a_request_with_no_absolute_time_names_no_window():
    assert _named("what did the school send") is None


def test_a_relative_period_is_named_by_the_text_but_only_the_slot_gives_its_window():
    concepts = mail_concepts("what did the school send last month")
    assert text_names_time(concepts) and named_window(concepts.time, TODAY) is None
    slot = TimeSlot("period", date(2026, 10, 1), date(2026, 10, 31))
    window = choose_window(concepts, _slots(slot), TODAY)
    assert (window.start, window.end, window.filters, window.label) == (
        date(2026, 10, 1), date(2026, 10, 31), True, "October 2026")


def test_the_text_beats_the_slot_on_an_absolute_period():
    slot = TimeSlot("period", date(2026, 10, 1), date(2026, 10, 31))
    window = choose_window(mail_concepts("mail from September"), _slots(slot), TODAY)
    assert window.start == date(2026, 9, 1) and window.filters


def test_an_upcoming_slot_with_no_time_words_adds_but_never_filters():
    window = choose_window(mail_concepts("the insurance policy"), _slots(TimeSlot("upcoming", None, None)), TODAY)
    assert (window.intent, window.start, window.end, window.filters) == (
        "upcoming", TODAY, date(2027, 11, 13), False)
    ends = choose_window(mail_concepts("the insurance policy"),
                         _slots(TimeSlot("upcoming", None, date(2027, 3, 31))), TODAY)
    assert ends.end == date(2027, 3, 31)


def test_a_period_slot_with_one_end_fills_the_other():
    only_end = choose_window(mail_concepts("last year"), _slots(TimeSlot("past", None, date(2025, 12, 31))), TODAY)
    assert only_end.start == date(2025, 1, 1)
    only_start = choose_window(mail_concepts("mail since this year"), _slots(TimeSlot("period", date(2026, 7, 1), None)), TODAY)
    assert only_start.end == TODAY
    assert choose_window(mail_concepts("mail"), _slots(TimeSlot("period", None, None)), TODAY) is None


def test_latest_is_a_window_that_never_filters_and_has_no_months():
    window = choose_window(mail_concepts("my last mail"), _slots(TimeSlot("latest", None, None)), TODAY)
    assert (window.intent, window.filters, window.start) == ("latest", False, None)
    assert window_months(window, TODAY) == () and in_window(booking(), window, TODAY)


def test_no_slot_and_no_named_time_is_no_window():
    assert choose_window(mail_concepts("the invoice"), None, TODAY) is None
    assert choose_window(mail_concepts("the invoice"), _slots(None), TODAY) is None


def test_the_booking_is_in_its_sent_month_and_in_its_event_month():
    june = TimeWindow("period", date(2025, 6, 1), date(2025, 6, 30), True, "June 2025")
    august = TimeWindow("period", date(2025, 8, 1), date(2025, 8, 31), True, "August 2025")
    july = TimeWindow("period", date(2025, 7, 1), date(2025, 7, 31), True, "July 2025")
    assert in_window(booking(), june, TODAY) and in_window(booking(), august, TODAY)
    assert not in_window(booking(), july, TODAY)


def test_an_unverified_point_is_never_a_time_filter():
    body = "Gentile Alex Doe, la sua prenotazione RM-55120 è confermata.\n"
    card = build("Prenotazione RM-55120", body, date(2025, 6, 2), "msg_rome02", "Albergo Immaginario",
                 "albergo.example")
    assert card.times and not card.times[0].verified
    august = TimeWindow("period", date(2025, 8, 1), date(2025, 8, 31), True, "August 2025")
    assert not in_window(card, august, TODAY)
    assert in_window(card, TimeWindow("period", date(2025, 6, 1), date(2025, 6, 30), True, "June 2025"), TODAY)


def test_upcoming_keeps_a_verified_validity_end_and_drops_a_past_event():
    window = TimeWindow("upcoming", TODAY, date(2027, 11, 13), False, "")
    assert in_window(policy(), window, TODAY)
    assert not in_window(booking(), window, TODAY)
    assert not in_window(invoice(), window, TODAY)      # no point of its own: the sent day is not ahead of anything


def test_upcoming_stops_at_the_window_end():
    window = TimeWindow("upcoming", TODAY, date(2027, 1, 31), False, "")
    assert not in_window(policy(), window, TODAY)


def test_a_three_year_window_touches_thirty_seven_months_oldest_first():
    window = TimeWindow("period", date(2024, 3, 15), date(2027, 3, 15), True, "")
    months = window_months(window, TODAY)
    assert len(months) == 37 and months[0] == "m:2024-03" and months[-1] == "m:2027-03"
    assert months == tuple(sorted(months))


def test_an_upcoming_window_counts_its_months_from_today():
    window = TimeWindow("upcoming", TODAY, date(2027, 3, 31), False, "")
    assert window_months(window, TODAY) == ("m:2026-11", "m:2026-12", "m:2027-01", "m:2027-02", "m:2027-03")


def test_a_year_boundary_rolls_the_month_keys_over():
    window = TimeWindow("period", date(2025, 11, 20), date(2026, 2, 2), True, "")
    assert window_months(window, TODAY) == ("m:2025-11", "m:2025-12", "m:2026-01", "m:2026-02")
