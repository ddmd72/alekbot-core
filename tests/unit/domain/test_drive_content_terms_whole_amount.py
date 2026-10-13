"""An amount whose cents are exactly .00 also queries its integer form (USER_DRIVE_INDEXING_RFC §24.6, arbiter R2
S-m2): "1,250.00" and "1250" are the same value, and a document that writes it without cents ("Total: 1250 EUR")
stores only "1250". Any other cents are a different value from the integer and still query their canonical form only.
The query side stays a subset of the index side for the same text."""
import pytest

from src.domain.drive_content_terms import content_terms, query_terms


@pytest.mark.parametrize("query", ["1,250.00", "1 250,00", "1.250,00", "1250.00", "1250,00", "€1,250.00"])
def test_a_whole_amount_with_zero_cents_meets_the_integer_in_a_document(query):
    assert set(query_terms(query)) & set(content_terms("Total: 1250 EUR"))
    assert set(query_terms(query)) == {"1250.00", "1250"}


@pytest.mark.parametrize("query", ["1,250.50", "1250.5", "1250,05"])
def test_other_cents_do_not_query_the_integer(query):
    assert "1250" not in query_terms(query)
    assert not set(query_terms(query)) & set(content_terms("Total: 1250 EUR"))


def test_the_canonical_form_is_still_the_most_specific():
    assert query_terms("1,250.00") == ("1250.00", "1250")


def test_a_short_whole_amount_adds_no_fragment_the_index_does_not_store():
    # "12.00": the index keeps no 2-digit integer part, so neither does the query
    assert query_terms("12.00") == ("12.00",)


def test_zero_cents_never_meet_a_hundred_twenty_five_thousand():
    assert not set(query_terms("1,250.00")) & set(content_terms("Total 125 000 EUR"))


@pytest.mark.parametrize("text", ["1,250.00", "Paid 1 250,00 € on 15.03.2024", "12.00", "INV-1250 for 1250.00",
                                  "100.00 and 2,000.00"])
def test_the_query_side_stays_a_subset_of_the_index_side(text):
    assert set(query_terms(text)) <= set(content_terms(text))
