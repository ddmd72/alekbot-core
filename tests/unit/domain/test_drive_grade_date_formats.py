"""Grader date formats from the first revision report (RFC §26.6 D37, ACCURACY_VERSION 2): bilingual and compact month
forms, ISO basic, and a day and month whose year is printed elsewhere. Synthetic texts only."""
from datetime import date

import pytest

from src.domain import drive_profile as dp
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, ProfileDate
from src.domain.drive_grade import TextEvidence, lenient_date
from src.domain.drive_index import ACCURACY_VERSION, DriveChunk

AS, NOT = Grade.IN_TEXT_AS, Grade.NOT_IN_TEXT


def _grade(iso, printed, text):
    return TextEvidence([text]).date_grade(iso, printed)[0]


@pytest.mark.parametrize("printed,iso", [
    ("27 ЛИП/JUL 22", date(2022, 7, 27)),
    ("19 ЛИС/NOV 66", date(1966, 11, 19)),
    ("03 СІЧ/JAN 31", date(2031, 1, 3)),
    ("05 ИЮЛ/JUL 22", date(2022, 7, 5)),
    ("27 ЛИП/JUL 2022", date(2022, 7, 27)),
    ("13JUN2024", date(2024, 6, 13)),
    ("13jun2024", date(2024, 6, 13)),
    ("13Jun2024", date(2024, 6, 13)),
    ("13ЧЕР2024", date(2024, 6, 13)),
    ("20261009T180724Z", date(2026, 10, 9)),
    ("20261009", date(2026, 10, 9)),
])
class TestFullDates:
    def test_matching_iso_is_in_text_as(self, printed, iso):
        assert _grade(iso, printed, f"Document. {printed} end.") is AS

    def test_a_different_day_month_or_year_is_refused(self, printed, iso):
        text = f"Document. {printed} end."
        for other in (date(iso.year, iso.month, iso.day % 28 + 1), date(iso.year + 1, iso.month, iso.day),
                      date(iso.year, iso.month % 12 + 1, iso.day)):
            assert _grade(other, printed, text) is NOT


class TestBilingualPair:
    def test_a_pair_that_disagrees_is_refused(self):
        assert lenient_date("27 ЛИП/AUG 22", date(2022, 7, 27)) is None
        assert lenient_date("27 ЛИП/AUG 22", date(2022, 8, 27)) is None
        assert _grade(date(2022, 7, 27), "27 ЛИП/AUG 22", "x 27 ЛИП/AUG 22 y") is NOT

    def test_a_two_digit_year_needs_the_expected_year(self):
        assert lenient_date("27 ЛИП/JUL 22") is None
        assert lenient_date("27 ЛИП/JUL 22", date(2023, 7, 27)) is None


class TestDayMonthWithoutYear:
    @pytest.mark.parametrize("printed", ["5 жовтня", "5 октября", "5 de octubre", "5 October", "October 5"])
    def test_in_text_as_when_the_year_is_elsewhere(self, printed):
        text = f"Заява від {printed}. Рік 2026."
        assert _grade(date(2026, 10, 5), printed, text) is AS

    def test_not_in_text_without_the_year(self):
        assert _grade(date(2026, 10, 5), "5 жовтня", "Заява від 5 жовтня.") is NOT

    def test_a_different_day_or_month_is_refused(self):
        text = "Заява від 5 жовтня. Рік 2026."
        assert _grade(date(2026, 10, 6), "5 жовтня", text) is NOT
        assert _grade(date(2026, 11, 5), "5 жовтня", text) is NOT

    def test_genitive_forms(self):
        assert _grade(date(2026, 11, 29), "29 листопада", "29 листопада. Рік 2026") is AS


class TestRegradeUnderVersion2:
    def test_a_profile_graded_not_in_text_is_regraded_in_text_as(self):
        assert ACCURACY_VERSION == 2
        text = "Passport. Born 27 ЛИП/JUL 22. Stamp 13JUN2024. Ref 20261009T180724Z."
        chunks = [DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=0, text=text, char_start=0,
                             char_end=len(text), kind="pdf")]
        profile = DriveProfile(title="Passport", what_it_is="A passport", language="en", doc_type="id_document",
                               dates=(ProfileDate("birth", date(2022, 7, 27), "27 ЛИП/JUL 22"),
                                      ProfileDate("stamp", date(2024, 6, 13), "13JUN2024"),
                                      ProfileDate("ref", date(2026, 10, 9), "20261009T180724Z")))
        stale = dp.grade(profile, [], "p.pdf")[0]          # no stored text: what version 1 left on a profile
        assert {g.grade for g in stale.graded if g.field == GradedField.DATE.value} == {NOT.value}
        regraded, _ = dp.grade(profile, chunks, "p.pdf")
        by = {g.value: g.grade for g in regraded.graded if g.field == GradedField.DATE.value}
        assert by == {"2022-07-27": AS.value, "2024-06-13": AS.value, "2026-10-09": AS.value}
