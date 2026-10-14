"""Written-out dates are exact terms too (USER_DRIVE_INDEXING_RFC §24.6; final review F5-5).

A date in a document or a question is often written with a month name: "1 March 2026", "1 березня 2026". Before, the
query kept only its year ("2026", a common term) and the document the same, so a question with a numeric date never
met a document with a written one. Both sides now recognise a day, a month name (en, es, fr, uk, ru, with case forms)
and a year, and give the same ISO term as "01.03.2026". A month and a year without a day give no date term on either
side: the query stays a subset of what the same text indexes."""
import pytest

from src.domain.drive_content_terms import content_terms, query_terms
from src.domain.drive_index import EXTRACTION_VERSION

MARCH_1 = "2026-03-01"


@pytest.mark.parametrize("text", [
    "1 March 2026", "01 March 2026", "1st March 2026", "1 Mar 2026", "1 mar. 2026", "March 1, 2026", "March 1st, 2026",
    "Mar 1, 2026", "1 of March 2026",
    "1 березня 2026", "1 березня 2026 року",
    "1 марта 2026", "1 марта 2026 г.", "1 МАРТА 2026",
    "1 de marzo de 2026", "1 marzo 2026", "1 de Marzo del 2026",
    "1er mars 2026", "1 mars 2026",
])
def test_a_written_out_date_is_its_iso_term_on_both_sides(text):
    assert query_terms(text) == (MARCH_1,)
    assert MARCH_1 in content_terms(f"The lease starts on {text} and runs a year.")


@pytest.mark.parametrize("text, iso", [
    ("15 февраля 2026", "2026-02-15"), ("20 квітня 2026", "2026-04-20"), ("3 June 2024", "2024-06-03"),
    ("12 May 2026", "2026-05-12"), ("1 de febrero de 2026", "2026-02-01"), ("14 août 2025", "2025-08-14"),
    ("14 aout 2025", "2025-08-14"), ("2 février 2026", "2026-02-02"), ("31 грудня 2025", "2025-12-31"),
    ("9 січня 2026", "2026-01-09"), ("5 septiembre 2026", "2026-09-05"), ("5 décembre 2026", "2026-12-05"),
])
def test_every_language_and_month_form(text, iso):
    assert query_terms(text) == (iso,)


def test_a_numeric_question_meets_a_written_document():
    document = set(content_terms("Двенадцать месяцев с 1 марта 2026 года."))
    for question in ("договор с 01.03.2026", "lease from 2026-03-01", "starts 1 March 2026"):
        assert set(query_terms(question)) & document, question


@pytest.mark.parametrize("text", ["March 2026", "березень 2026", "березень", "31 February 2026", "1 Marchy 2026",
                                  "1 mayor 2026"])
def test_no_day_or_no_real_date_gives_no_date_term(text):
    assert not any(len(t) == 10 and t[4] == "-" for t in query_terms(text))
    assert set(query_terms(text)) <= set(content_terms(text))


@pytest.mark.parametrize("text", [
    "Paid 1 250,00 € on 1 March 2026 for invoice INV-2024/0012.", "Сплатити до 20 квітня 2026: 1 030,00 грн",
    "1er mars 2026, 15.03.2026 et 2026-03-20", "May 5, 2026 May 6, 2026",
])
def test_the_query_stays_a_subset_of_what_the_same_text_indexes(text):
    assert set(query_terms(text)) <= set(content_terms(text))


def test_the_extraction_version_moved():
    assert EXTRACTION_VERSION == 3   # stored content_terms lack the written-out dates: chunks are re-derived
