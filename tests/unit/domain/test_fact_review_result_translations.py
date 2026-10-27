"""The review result carries the validated translations."""
import pytest

from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_result import FactReviewResult, decode_review_result, encode_review_result


def test_the_result_round_trips_its_translations():
    record = QuoteTranslation("uk", quote_digest("Alex plays tennis on Sundays"), "Алекс грає в теніс по неділях")
    result = FactReviewResult(translations=(record,))
    encoded = encode_review_result(result)
    assert encoded["translations"] == [{"language": "uk", "source_digest": record.source_digest, "text": record.text}]
    assert decode_review_result(encoded) == result
    assert decode_review_result({}).translations == ()


@pytest.mark.parametrize("entry", [
    {"language": "uk"}, {"language": "UK", "source_digest": "0123456789abcdef", "text": "x"}, "text", 5,
])
def test_a_malformed_translation_entry_is_refused(entry):
    with pytest.raises(ValueError):
        decode_review_result({"translations": [entry]})
