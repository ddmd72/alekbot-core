"""The content exact-token normaliser (USER_DRIVE_INDEXING_RFC §24.6): one function for chunks and queries, so the
same amount, date or id meets whatever separators each side wrote."""
import pytest

from src.domain.drive_content_terms import CONTENT_TERMS_PER_CHUNK, content_terms, query_terms


class TestAmounts:
    @pytest.mark.parametrize("written", [
        "1 250,00",          # RU / UA / FR: space groups, comma decimals
        "1 250,00",     # the same with a no-break space
        "1 250,00",     # and a narrow no-break space
        "1.250,00",          # ES / DE: dot groups
        "1,250.00",          # EN
        "1'250.00",          # CH
        "1250.00",
        "1250,00",
    ])
    def test_every_style_meets_one_canonical_form(self, written):
        terms = content_terms(f"Total: {written} EUR")
        assert "1250.00" in terms and "1250" in terms

    def test_one_decimal_is_padded(self):
        assert "1250.50" in content_terms("1250,5 €")

    def test_millions(self):
        assert "1250000.50" in content_terms("1 250 000,50 грн")

    def test_small_amount_keeps_only_the_decimal_form(self):
        terms = content_terms("Fee 12,50 EUR")
        assert "12.50" in terms and "12" not in terms

    def test_currency_sign_before_the_amount(self):
        assert "1250.00" in content_terms("€1,250.00")

    def test_query_finds_the_chunk_written_in_another_style(self):
        assert set(query_terms("invoice for 1250.00")) & set(content_terms("Сума: 1 250,00 грн"))

    def test_integer_amounts_meet_through_their_digit_run(self):
        assert "1250" in set(query_terms("1250 euro")) & set(content_terms("Importe 1.250,00 €"))

    def test_long_decimals_are_not_amounts(self):
        assert not any("." in t for t in content_terms("pi is 3.14159"))


class TestDates:
    def test_iso(self):
        assert "2024-03-15" in content_terms("issued 2024-03-15")

    @pytest.mark.parametrize("written", ["15.03.2024", "15/03/2024", "15-03-2024"])
    def test_day_first_unambiguous(self, written):
        terms = content_terms(written)
        assert "2024-03-15" in terms and "2024-15-03" not in terms

    def test_month_first_unambiguous(self):
        assert "2024-03-15" in content_terms("03/15/2024")

    def test_ambiguous_dates_get_both_readings(self):
        terms = content_terms("05/03/2024")
        assert {"2024-03-05", "2024-05-03"} <= set(terms)

    def test_impossible_dates_are_dropped(self):
        assert not any(t.count("-") == 2 for t in content_terms("31.02.2024"))

    def test_a_date_is_not_an_amount(self):
        assert "15.03" not in content_terms("15.03.2024")

    def test_query_date_meets_chunk_date(self):
        assert "2024-03-15" in set(query_terms("from 2024-03-15")) & set(content_terms("від 15.03.2024"))


class TestIds:
    def test_id_with_separators(self):
        terms = content_terms("Invoice INV-2024/0012 attached")
        assert {"inv20240012", "20240012", "2024", "0012"} <= set(terms)

    def test_id_matches_without_separators_in_the_query(self):
        assert "inv20240012" in set(query_terms("INV20240012")) & set(content_terms("№ INV-2024/0012"))

    def test_cyrillic_ids(self):
        assert "ав123456" in content_terms("Паспорт АВ-123456")

    def test_short_digit_runs_are_dropped(self):
        assert content_terms("page 12 of 34") == ()

    def test_words_without_digits_are_not_terms(self):
        assert content_terms("A plain sentence, nothing to see.") == ()


class TestShape:
    def test_sorted_and_unique(self):
        terms = content_terms("1250.00 and 1 250,00 and INV-77 7777")
        assert list(terms) == sorted(set(terms))

    def test_cap_keeps_the_longest_terms(self):
        text = " ".join(f"{n:03d}" for n in range(100, 1000)) + " ABC-123456789012"
        terms = content_terms(text)
        assert len(terms) == CONTENT_TERMS_PER_CHUNK
        assert "abc123456789012" in terms and "123456789012" in terms

    def test_query_terms_most_specific_first(self):
        terms = query_terms("INV-2024/0012 15.03.2024")
        assert list(terms) == sorted(terms, key=lambda t: (-len(t), t))
        assert terms[0] == "inv20240012"

    def test_empty(self):
        assert content_terms("") == () and query_terms("") == ()
