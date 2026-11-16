"""Email search v2 time reading after the G3 review (slice 8): a month or a day named without a year takes its year from
the slot's span (C1), a day with its month and no year is one day, never a month and a number word (C2), and the
declined Russian and Ukrainian time words and the Portuguese time and mail words are removed from the words and name a
time (C2). Today is fixed at 2026-11-16 (Monday). Invented cast only."""
from datetime import date

import pytest

from src.domain.drive_keys import TIME_WORDS
from src.domain.email_search_params import MAIL_GENERIC_WORDS
from src.domain.email_search_query import NamedDay, mail_concepts, parse_query_slots
from src.domain.email_search_time import choose_window, text_names_time

TODAY = date(2026, 11, 16)


def _window(text, time=None):
    slots = parse_query_slots({"time": time}, text) if time is not None else None
    return choose_window(mail_concepts(text), slots, TODAY)


def _span(window):
    return window.start, window.end, window.filters


# -- C1: a month without a year takes the slot's year ------------------------------------------------------------------

def test_the_rome_booking_from_august_last_year_is_august_2025():
    window = _window("my Albergo Immaginario booking RM-55120 from August last year",
                     {"intent": "period", "from": "2025-08-01", "to": "2025-08-31"})
    assert _span(window) == (date(2025, 8, 1), date(2025, 8, 31), True) and window.label == "August 2025"


def test_the_dentist_appointment_in_december_is_the_coming_one():
    text = "my appointment with Dr. Marta Quillen at Clínica Ribeira in December"
    for slot in ({"intent": "upcoming", "from": "2026-11-16", "to": ""},
                 {"intent": "upcoming", "from": "2026-11-16", "to": "2026-12-31"}):
        assert _span(_window(text, slot)) == (date(2026, 12, 1), date(2026, 12, 31), True)


def test_without_a_slot_a_bare_month_is_still_the_latest_started_one():
    assert _span(_window("my Albergo Immaginario booking from August")) == (date(2026, 8, 1), date(2026, 8, 31), True)
    assert _span(_window("Clínica Ribeira appointment in December")) == (date(2025, 12, 1), date(2025, 12, 31), True)


def test_a_slot_that_misses_the_month_does_not_move_it():
    window = _window("Clínica Ribeira appointment in December", {"intent": "period", "from": "2026-03-01",
                                                                 "to": "2026-03-31"})
    assert _span(window) == (date(2025, 12, 1), date(2025, 12, 31), True)


def test_a_month_with_its_year_wins_over_the_slot():
    window = _window("Albergo Immaginario mail from August 2025", {"intent": "period", "from": "2024-08-01",
                                                                   "to": "2024-08-31"})
    assert _span(window) == (date(2025, 8, 1), date(2025, 8, 31), True)


def test_a_year_in_the_text_keeps_a_bare_month_on_todays_reading():
    window = _window("Example School letters of 2025 and from March",
                     {"intent": "period", "from": "2024-03-01", "to": "2024-03-31"})
    assert window.start == date(2025, 1, 1) and window.end == date(2026, 3, 31)


# -- C2: a day with its month and no year ------------------------------------------------------------------------------

@pytest.mark.parametrize("text,day", [
    ("лист від 5 жовтня", NamedDay(10, 5)),
    ("письмо от 12 марта", NamedDay(3, 12)),
    ("the mail of October 5", NamedDay(10, 5)),
    ("the 5th of October mail", NamedDay(10, 5)),
    ("correo del 12 de marzo", NamedDay(3, 12)),
])
def test_a_day_and_its_month_are_one_named_day_and_no_word(text, day):
    concepts = mail_concepts(text)
    assert concepts.days == (day,)
    assert concepts.time == () and concepts.topic == ()
    assert text_names_time(concepts)


def test_a_named_day_is_one_day_the_latest_by_today():
    window = _window("лист від 5 жовтня")
    assert _span(window) == (date(2026, 10, 5), date(2026, 10, 5), True) and window.label == "5 Oct 2026"
    assert _span(_window("Example School mail of December 3")) == (date(2025, 12, 3), date(2025, 12, 3), True)


def test_a_named_day_takes_the_slots_year():
    window = _window("лист від 5 жовтня", {"intent": "period", "from": "2025-10-05", "to": "2025-10-05"})
    assert _span(window) == (date(2025, 10, 5), date(2025, 10, 5), True)


def test_a_day_with_its_year_stays_a_full_date():
    concepts = mail_concepts("лист від 5 жовтня 2026")
    assert concepts.days == () and [c.word for c in concepts.time] == ["5 жовтня 2026"]


def test_a_day_no_year_has_is_not_a_day():
    concepts = mail_concepts("Example School mail of 31 April")
    assert concepts.days == ()


def test_a_named_day_beside_an_invoice_key_leaves_the_key_a_topic_word():
    concepts = mail_concepts("Oct 5th invoice FA-0137")
    assert concepts.days == (NamedDay(10, 5),) and "fa-0137" in [c.word for c in concepts.topic]


# -- C2: declined time words and Portuguese words ----------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "письмо со следующей недели", "письма следующего месяца", "письмо прошлым месяцем", "в следующую субботу неделей",
    "за последним годом", "текущей неделей", "лист наступного тижня", "листи минулого місяця", "лист минулій неділі",
    "останньому тижнем", "лист наступної неділі місяцем",
])
def test_declined_time_words_are_removed_and_name_a_time(phrase):
    concepts = mail_concepts(phrase)
    removed = {w for w in concepts.ignored if w in TIME_WORDS}
    assert removed and not any(c.word in TIME_WORDS for c in concepts.topic)
    assert text_names_time(concepts)


@pytest.mark.parametrize("word", [
    "следующий", "следующей", "следующую", "следующих", "прошлым", "месяцем", "неделях", "годами", "последнему",
    "текущей", "наступний", "наступного", "наступній", "минулій", "минулу", "тижнем", "місяцях", "роками",
    "останньому", "поточної",
])
def test_the_declined_forms_are_time_words(word):
    assert word in TIME_WORDS


@pytest.mark.parametrize("text", ["fatura do mês passado", "mensagem de ontem", "e-mail de hoje",
                                  "faturas de próxima semana", "mensagens do ano passado", "o que chegou amanhã"])
def test_portuguese_time_and_mail_words_are_removed_and_name_a_time(text):
    concepts = mail_concepts(text)
    assert concepts.topic == ()
    assert text_names_time(concepts)


def test_a_portuguese_relative_period_filters_by_the_slot():
    window = _window("fatura do mês passado", {"intent": "period", "from": "2026-10-01", "to": "2026-10-31"})
    assert _span(window) == (date(2026, 10, 1), date(2026, 10, 31), True)


def test_the_portuguese_mail_words_are_mail_words():
    assert {"mensagem", "mensagens", "fatura", "faturas", "correio"} <= MAIL_GENERIC_WORDS
    assert {"mês", "ano", "semana", "ontem", "hoje", "amanhã", "próximo", "próxima", "passado"} <= TIME_WORDS


# -- an open upcoming slot reaches the same month next year: the coming one is meant ---------------------------------

OPEN_UPCOMING = {"intent": "upcoming", "from": "", "to": ""}


@pytest.mark.parametrize("text,month", [
    ("my Clínica Ribeira appointment in November", (date(2026, 11, 1), date(2026, 11, 30))),
    ("my Clínica Ribeira appointment in October", (date(2027, 10, 1), date(2027, 10, 31))),
    ("my Clínica Ribeira appointment in December", (date(2026, 12, 1), date(2026, 12, 31))),
])
def test_an_open_upcoming_slot_gives_a_month_its_next_occurrence(text, month):
    window = _window(text, OPEN_UPCOMING)
    assert (window.start, window.end) == month and window.filters


def test_an_open_upcoming_slot_gives_a_past_day_of_this_month_next_year():
    window = _window("my Clínica Ribeira appointment on 10 November", OPEN_UPCOMING)
    assert _span(window) == (date(2027, 11, 10), date(2027, 11, 10), True)


def test_a_look_back_slot_still_takes_the_latest_year():
    window = _window("Clínica Ribeira invoices from November",
                     {"intent": "past", "from": "2024-11-01", "to": "2026-11-16"})
    assert (window.start, window.end) == (date(2026, 11, 1), date(2026, 11, 30))
