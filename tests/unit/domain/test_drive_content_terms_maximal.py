"""Query terms are maximal forms only; fragments stay on the index side (USER_DRIVE_INDEXING_RFC §24.6, review D-M1).

A query term is matched by one array-contains query, so a fragment of what the user typed ("2024" out of an invoice
number, "125000" out of "1250.00") would match documents that share only the fragment. The index side keeps the
fragments, so a query for a part of an id still finds the whole one."""
import pytest

from src.domain.drive_content_terms import content_terms, query_terms

# A synthetic IBAN-shaped string (no real account), assembled so no source line holds it whole.
IBAN_GROUPS = ("ZZ07", "0000", "1111", "2222", "3333", "44")
IBAN = " ".join(IBAN_GROUPS)
IBAN_JOINED = "".join(IBAN_GROUPS).casefold()


class TestQueryEmitsOnlyMaximalForms:
    def test_an_id_is_its_joined_form_only(self):
        assert query_terms("invoice INV-2024/0012") == ("inv20240012",)

    def test_a_digit_id_is_its_joined_digits_only(self):
        assert query_terms("ref 2024/0012") == ("20240012",)

    def test_a_date_is_its_iso_form_only(self):
        assert query_terms("paid on 15.03.2024") == ("2024-03-15",)

    def test_an_ambiguous_date_is_both_readings_only(self):
        assert set(query_terms("05/03/2024")) == {"2024-03-05", "2024-05-03"}

    def test_an_iso_date_is_not_also_its_joined_digits(self):
        assert query_terms("2024-03-15") == ("2024-03-15",)

    def test_a_decimal_amount_is_its_canonical_form_plus_a_zero_cents_integer(self):
        # zero cents also query the integer, the same value (R2 S-m2); any other cents stay the canonical form only
        assert query_terms("invoice for 1250.00") == ("1250.00", "1250")
        assert query_terms("invoice for 1250.50") == ("1250.50",)

    def test_a_grouped_amount_is_its_canonical_form_plus_a_zero_cents_integer(self):
        assert query_terms("1 250,00 €") == ("1250.00", "1250")
        assert query_terms("1 250,50 €") == ("1250.50",)

    def test_a_plain_number_is_itself(self):
        assert query_terms("1250 euro") == ("1250",)

    def test_a_dotted_number_that_is_no_date_or_amount_gives_no_term(self):
        # "3.14159": no joined digits (it holds a '.'), and its runs are fragments
        assert query_terms("pi 3.14159") == ()


class TestNoJoinedDigitsAcrossDotOrComma:
    def test_a_decimal_amount_does_not_index_its_digits_joined(self):
        assert "125000" not in content_terms("Total 1250.00 EUR")

    def test_a_comma_decimal_does_not_index_its_digits_joined(self):
        assert "125000" not in content_terms("Сума 1250,00 грн")

    def test_a_dotted_date_does_not_index_its_digits_joined(self):
        assert "15032024" not in content_terms("від 15.03.2024")

    def test_a_dashed_id_still_indexes_its_joined_digits(self):
        assert "20240012" in content_terms("№ 2024-0012")


class TestBothSidesMeet:
    @pytest.mark.parametrize("query", ["1250.00", "1250,00", "1250"])
    def test_the_grouped_amount_in_a_document_meets_the_query(self, query):
        assert set(query_terms(query)) & set(content_terms("Сума: 1 250,00 €"))

    def test_a_hundred_twenty_five_thousand_does_not_meet_twelve_fifty(self):
        assert not set(query_terms("1250.00")) & set(content_terms("Total 125 000 EUR"))

    def test_twelve_fifty_does_not_meet_a_hundred_twenty_five_thousand(self):
        assert not set(query_terms("125 000")) & set(content_terms("Total 1250.00 EUR"))

    def test_a_part_of_an_id_in_the_query_still_finds_the_whole_id(self):
        assert set(query_terms("0012")) & set(content_terms("Invoice INV-2024/0012"))

    @pytest.mark.parametrize("text", [
        "invoice INV-2024/0012", "ref 2024/0012", "paid on 15.03.2024", "05/03/2024", "2024-03-15",
        "invoice for 1250.00", "1 250,00 €", "1250 euro", "Паспорт АВ-123456", "€1,250.00", "Fee 12,50",
        "IBAN " + IBAN, "tel +1 202 555 0100", "+44 77 00 90 00 12",
    ])
    def test_every_query_term_is_a_term_the_same_text_indexes(self, text):
        # one normaliser: a query term is always one of the forms the index stores for the same text
        assert set(query_terms(text)) <= set(content_terms(text))


class TestWhitespaceSeparatedDigitGroups:
    def test_an_iban_indexes_its_joined_form(self):
        assert IBAN_JOINED in content_terms(f"IBAN: {IBAN}.")

    def test_an_iban_in_the_query_is_its_joined_form_only(self):
        assert query_terms(IBAN) == (IBAN_JOINED,)

    def test_an_iban_written_without_spaces_meets_the_grouped_one(self):
        assert set(query_terms("".join(IBAN_GROUPS))) & set(content_terms(IBAN))

    def test_a_phone_indexes_its_joined_digits(self):
        assert "12025550100" in content_terms("Tel. +1 202 555 0100")

    def test_a_french_phone_in_pairs_indexes_its_joined_digits(self):
        assert "447700900012" in content_terms("Portable : +44 77 00 90 00 12")

    def test_a_phone_typed_without_spaces_meets_the_grouped_one(self):
        assert set(query_terms("447700900012")) & set(content_terms("Portable : +44 77 00 90 00 12"))

    def test_short_groups_are_not_joined(self):
        # 8 digits in all: below the joined minimum
        assert "12345678" not in content_terms("page 12 34 56 78")

    def test_an_amount_with_cents_is_not_a_digit_group_run(self):
        terms = content_terms("Total 1 250 000 000,50")
        assert "1250000000.50" in terms and "1250000000" in terms
