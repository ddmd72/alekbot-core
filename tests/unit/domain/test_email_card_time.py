"""The card's time envelope (email index v2 §2.4; card slice 1): issued == sent dropped, ranges, yearless dates,
weekdays, the drive grade fallback, profile dates, month keys and the display `when`. Invented dates only (the Rome
trip 10–14 Aug 2025, the parents' meeting Tuesday 3 March 2026, the motor policy 01/04/2026–31/03/2027)."""
from datetime import date

import pytest

from src.domain.drive_grade import TextEvidence
from src.domain.drive_index_params import PROFILE_DATES_MAX
from src.domain.email_card import AnswerDate, TimePoint, When
from src.domain.email_card_time import month_keys, profile_dates, render_when, time_envelope, when_of

SENT = date(2025, 6, 2)


def _one(label, start, end, printed, text, sent=SENT):
    points = time_envelope([AnswerDate(label=label, start=start, end=end, printed=printed)], sent,
                           TextEvidence([text]))
    assert len(points) == 1
    return points[0]


# -- issued == sent -------------------------------------------------------------------------------------------------

def test_an_issued_date_equal_to_the_sent_day_is_dropped():
    dates = [AnswerDate("issued", SENT, None, "2 June 2025"), AnswerDate("event", date(2025, 8, 10), None, "")]
    points = time_envelope(dates, SENT, TextEvidence(["Issued 2 June 2025."]))
    assert [p.label for p in points] == ["event"]


def test_an_issued_date_on_another_day_or_another_label_on_the_sent_day_stays():
    dates = [AnswerDate("issued", date(2025, 6, 1), None, ""), AnswerDate("event", SENT, None, ""),
             AnswerDate("issued", SENT, date(2025, 6, 5), "")]
    assert len(time_envelope(dates, SENT, TextEvidence(["text"]))) == 3


# -- ranges ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("printed", [
    "10–14 Aug 2025", "10-14 Aug 2025", "10 - 14 August 2025", "10/08 – 14/08/2025", "Aug 10 – 14, 2025",
    "10 Aug 2025 to 14 Aug 2025", "del 10 al 14 de agosto de 2025", "10.08.2025 - 14.08.2025",
])
def test_a_printed_span_is_read_from_its_one_form(printed):
    p = _one("event", date(2025, 8, 10), date(2025, 8, 14), printed, f"Your stay: {printed}.")
    assert p == TimePoint("event", date(2025, 8, 10), date(2025, 8, 14), printed, True)


def test_a_span_over_the_new_year_with_the_year_printed_once():
    p = _one("event", date(2025, 12, 28), date(2026, 1, 3), "28 Dec – 3 Jan 2026", "Stay 28 Dec – 3 Jan 2026.")
    assert p.verified and (p.start, p.end) == (date(2025, 12, 28), date(2026, 1, 3))
    p = _one("event", date(2025, 12, 28), date(2026, 1, 3), "28 Dec 2025 – 3 Jan", "Stay 28 Dec 2025 – 3 Jan.")
    assert p.verified


def test_a_validity_span_with_full_numeric_dates():
    printed = "01/04/2026–31/03/2027"
    p = _one("valid_until", date(2026, 4, 1), date(2027, 3, 31), printed, f"Policy MP-2026-118204, {printed}.")
    assert p.verified


def test_a_span_the_model_misread_stays_unverified_with_the_models_days():
    p = _one("event", date(2025, 8, 10), date(2025, 8, 15), "10–14 Aug 2025", "Stay 10–14 Aug 2025.")
    assert p == TimePoint("event", date(2025, 8, 10), date(2025, 8, 15), "10–14 Aug 2025", False)


def test_a_span_printed_for_a_single_date_or_the_reverse_is_unread():
    assert not _one("event", date(2025, 8, 10), None, "10–14 Aug 2025", "Stay 10–14 Aug 2025.").verified
    assert not _one("event", date(2025, 8, 10), date(2025, 8, 14), "10 Aug 2025", "From 10 Aug 2025.").verified


# -- yearless -------------------------------------------------------------------------------------------------------

def test_a_yearless_date_takes_the_next_occurrence_on_or_after_the_sent_day():
    sent = date(2026, 2, 20)
    p = _one("event", date(2025, 3, 3), None, "3 March", "Meeting on 3 March at Example School.", sent)
    assert p == TimePoint("event", date(2026, 3, 3), date(2026, 3, 3), "3 March", True)   # code's year wins
    p = _one("event", date(2026, 3, 3), None, "3 March", "Meeting on 3 March.", date(2026, 12, 10))
    assert p.verified and p.start == date(2027, 3, 3)
    p = _one("event", date(2026, 7, 14), None, "14/07", "Clara's birthday on 14/07.", date(2026, 10, 1))
    assert p.verified and p.start == date(2027, 7, 14)           # a numeric day/month rolls to the next year


def test_a_yearless_issued_date_takes_the_previous_occurrence():
    sent = date(2026, 2, 20)
    p = _one("issued", date(2026, 9, 15), None, "15 Sept", "Invoice FA-0137 of 15 Sept.", date(2026, 10, 1))
    assert p.verified and p.start == date(2026, 9, 15)
    p = _one("issued", date(2025, 9, 15), None, "15/09", "Invoice FA-0137 of 15/09.", date(2026, 10, 1))
    assert p.verified and p.start == date(2026, 9, 15)           # numeric day/month: code's year wins
    p = _one("issued", date(2026, 9, 15), None, "15 Sept", "Invoice of 15 Sept.", sent)
    assert p.verified and p.start == date(2025, 9, 15)
    p = _one("issued", date(2026, 2, 20), None, "20 Feb", "Invoice of 20 Feb.", date(2026, 2, 21))
    assert p.verified and p.start == date(2026, 2, 20)


def test_a_yearless_date_on_the_sent_day_is_that_day():
    p = _one("due", SENT, None, "2 June", "Pay by 2 June.")
    assert p.verified and p.start == SENT


def test_a_yearless_span_follows_its_start_and_crosses_the_new_year():
    p = _one("event", date(2025, 8, 10), date(2025, 8, 14), "10–14 Aug", "Stay 10–14 Aug.")
    assert p.verified and (p.start, p.end) == (date(2025, 8, 10), date(2025, 8, 14))
    p = _one("event", date(2025, 12, 28), date(2026, 1, 3), "28 Dec – 3 Jan", "Stay 28 Dec – 3 Jan.",
             date(2025, 11, 1))
    assert p.verified and (p.start, p.end) == (date(2025, 12, 28), date(2026, 1, 3))


def test_a_yearless_29_february_finds_the_next_leap_year():
    p = _one("event", date(2028, 2, 29), None, "29 Feb", "Event on 29 Feb.", date(2025, 3, 1))
    assert p.verified and p.start == date(2028, 2, 29)


def test_a_yearless_date_whose_day_disagrees_is_unverified():
    p = _one("event", date(2026, 3, 4), None, "3 March", "Meeting on 3 March.", date(2026, 2, 20))
    assert p == TimePoint("event", date(2026, 3, 4), date(2026, 3, 4), "3 March", False)
    p = _one("event", date(2025, 8, 10), date(2025, 8, 15), "10–14 Aug", "Stay 10–14 Aug.")
    assert not p.verified


# -- weekdays -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("printed", ["Tuesday 3 March 2026", "Tue, 3 Mar 2026", "martes 3 de marzo de 2026",
                                     "terça-feira, 3 de março de 2026", "Dienstag, 03.03.2026",
                                     "вівторок, 03.03.2026"])
def test_a_weekday_that_falls_on_the_date_keeps_it_verified(printed):
    assert _one("event", date(2026, 3, 3), None, printed, f"Parents' meeting: {printed}.").verified


@pytest.mark.parametrize("printed", ["Friday 3 March 2026", "Wed, 3 Mar 2026", "Friday 3 March"])
def test_a_weekday_that_disagrees_leaves_the_date_unverified(printed):
    p = _one("event", date(2026, 3, 3), None, printed, f"Parents' meeting: {printed}.", date(2026, 2, 20))
    assert not p.verified and p.start == date(2026, 3, 3)


def test_each_end_of_a_span_checks_its_own_weekday():
    printed = "Sunday 10 – Thursday 14 Aug 2025"
    assert _one("event", date(2025, 8, 10), date(2025, 8, 14), printed, f"Stay {printed}.").verified
    printed = "Sunday 10 – Friday 14 Aug 2025"
    assert not _one("event", date(2025, 8, 10), date(2025, 8, 14), printed, f"Stay {printed}.").verified


# -- printed in the mail, unreadable forms ------------------------------------------------------------------------

def test_a_printed_form_not_in_the_mail_is_automatic():
    assert not _one("event", date(2025, 8, 10), None, "10 Aug 2025", "Stay in August.").verified
    assert not _one("event", date(2025, 8, 10), None, "", "Stay 10 Aug 2025.").verified


def test_a_full_date_that_reads_differently_is_unverified():
    assert not _one("event", date(2025, 8, 11), None, "10 Aug 2025", "Stay 10 Aug 2025.").verified
    assert _one("event", date(2025, 8, 10), None, "10/08/25", "Stay 10/08/25.").verified   # two-digit year


@pytest.mark.parametrize("printed, text, verified", [
    ("20250810", "Ref date 20250810.", True),               # read by the drive's date grade
    ("tomorrow", "See you tomorrow.", False),
    ("10 Aug 15 Aug 2025", "From 10 Aug 15 Aug 2025.", False),
    ("Aug Sep 2025", "Aug Sep 2025.", False),
    ("2025", "In 2025.", False),
    ("10/08/1", "On 10/08/1.", False),
    ("10/08/12345", "On 10/08/12345.", False),
    ("32/08/2025", "On 32/08/2025.", False),
    ("10/13/2025", "On 10/13/2025.", False),
    ("2025-08-10-11", "On 2025-08-10-11.", True),           # the drive grade: the ISO day is among the text's terms
    ("1 2 3 4", "Code 1 2 3 4.", False),
    ("123/08/2025", "On 123/08/2025.", False),
    ("10 Aug 2025 2026", "On 10 Aug 2025 2026.", True),     # the drive grade: the text prints the day
    ("14, 2025", "On 14, 2025.", False),
    ("10 11 Aug 2025", "On 10 11 Aug 2025.", False),
])
def test_forms_code_cannot_read_fall_back_to_the_drive_grade_for_a_single_day(printed, text, verified):
    assert _one("event", date(2025, 8, 10), None, printed, text).verified is verified


def test_an_unreadable_span_is_never_verified():
    p = _one("event", date(2025, 8, 10), date(2025, 8, 14), "Sun 10 to Thu 14", "Sun 10 to Thu 14.")
    assert not p.verified
    p = _one("event", date(2025, 8, 10), date(2025, 8, 14), "10 to 14 to 15", "10 to 14 to 15.")
    assert not p.verified


def test_an_iso_printed_date_and_an_iso_span():
    assert _one("due", date(2025, 8, 10), None, "2025-08-10", "Due 2025-08-10.").verified
    assert _one("event", date(2025, 8, 10), date(2025, 8, 14), "2025-08-10..2025-08-14",
                "Stay 2025-08-10..2025-08-14.").verified


def test_a_clock_time_in_the_printed_form_is_ignored():
    assert _one("event", date(2026, 3, 3), None, "3 March 2026, 18:30", "Meeting 3 March 2026, 18:30.").verified


def test_a_yearless_form_with_no_month_is_unread():
    assert not _one("event", date(2025, 8, 10), None, "the 10th", "On the 10th.").verified


# -- profile dates, month keys, when --------------------------------------------------------------------------------

def _p(label, start, end=None, verified=True, printed=""):
    return TimePoint(label, start, end or start, printed, verified)


def test_profile_dates_take_every_start_then_span_ends_within_the_drive_cap():
    points = [_p("event", date(2025, 8, 10), date(2025, 8, 14)), _p("due", date(2025, 7, 1)),
              _p("due", date(2025, 7, 1))]
    got = profile_dates(points)
    assert [(d.label, d.iso) for d in got] == [("event", date(2025, 8, 10)), ("due", date(2025, 7, 1)),
                                               ("event", date(2025, 8, 14))]
    many = [_p("event", date(2025, 8, i), date(2025, 9, i)) for i in range(1, 7)]
    got = profile_dates(many)
    assert len(got) == PROFILE_DATES_MAX and all(d.iso.month == 8 for d in got)


def test_month_keys_hold_verified_points_ends_and_the_sent_month():
    points = [_p("event", date(2025, 8, 10), date(2025, 9, 2)), _p("due", date(2025, 7, 1), verified=False)]
    assert month_keys(points, SENT) == ("m:2025-06", "m:2025-08", "m:2025-09")
    assert month_keys([], SENT) == ("m:2025-06",)


def test_when_prefers_event_then_due_then_validity_then_sent():
    event, due = _p("event", date(2025, 8, 10), date(2025, 8, 14)), _p("due", date(2025, 7, 1))
    valid = _p("valid_until", date(2027, 3, 31))
    assert when_of([valid, due, event], SENT) == When("event", date(2025, 8, 10), date(2025, 8, 14))
    assert when_of([valid, due], SENT) == When("due", date(2025, 7, 1), date(2025, 7, 1))
    assert when_of([valid], SENT) == When("valid_until", date(2027, 3, 31), date(2027, 3, 31))
    assert when_of([_p("event", date(2025, 8, 10), verified=False), _p("issued", date(2025, 5, 1))], SENT) == \
        When("sent", SENT, SENT)


@pytest.mark.parametrize("when, text", [
    (When("event", date(2025, 8, 14), date(2025, 8, 14)), "event 14 Aug 2025"),
    (When("event", date(2025, 8, 10), date(2025, 8, 14)), "event 10–14 Aug 2025"),
    (When("event", date(2025, 7, 28), date(2025, 8, 3)), "event 28 Jul – 3 Aug 2025"),
    (When("event", date(2025, 12, 28), date(2026, 1, 3)), "event 28 Dec 2025 – 3 Jan 2026"),
    (When("valid_until", date(2027, 3, 31), date(2027, 3, 31)), "valid until 31 Mar 2027"),
    (When("sent", SENT, SENT), "sent 2 Jun 2025"),
])
def test_render_when(when, text):
    assert render_when(when) == text


def test_a_yearless_day_no_month_has_is_unverified():
    assert not _one("event", date(2025, 4, 30), None, "31 April", "Event on 31 April.").verified
    p = _one("event", date(2025, 9, 28), date(2025, 9, 30), "28–31 Sep", "Stay 28–31 Sep.")
    assert not p.verified and p.end == date(2025, 9, 30)
