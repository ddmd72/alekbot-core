"""Facts S4 · T7b fix round (S2): `review_labels`, the one domain call for where the review's texts come from (the current
cached set through `label_source`, and the language of the UI strings)."""
from typing import Any, Dict

import pytest

from src.domain.fact_review_language import CANONICAL_LABELS, check_labels, labels_digest, review_labels
from src.domain.fact_review_state import FactReviewState
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage

# P2 revised (C11): the options of one question kind must differ, and the last word of a key does not tell
# memory_option_happened from memory_option_not_happened; the key without its prefix does
RU: Dict[str, str] = {key: f"ру {key.removeprefix('memory_')}" for key in CANONICAL_LABELS}
BAD_RU = {**RU, UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Да 1"}


def state(**over: Any) -> FactReviewState:
    return FactReviewState(account_id="acc", **over)


def test_the_good_labels_are_good():
    assert check_labels(RU) is None and check_labels(BAD_RU) is not None


def test_current_good_labels_are_the_models():
    source, language, cached = review_labels("ru", state(labels={"ru": RU}, label_digests={"ru": labels_digest()}),
                                             LanguageCode.UK)

    assert source == "model" and language is None and dict(cached or {}) == RU


def test_a_matching_digest_with_bad_texts_falls_back_to_the_ui_language():
    result = review_labels("ru", state(labels={"ru": BAD_RU}, label_digests={"ru": labels_digest()}), LanguageCode.ES)

    assert result == ("fallback", LanguageCode.ES, None)


def test_an_old_digest_falls_back():
    result = review_labels("ru", state(labels={"ru": RU}, label_digests={"ru": "0123456789abcdef"}), LanguageCode.UK)

    assert result == ("fallback", LanguageCode.UK, None)


def test_no_digest_field_falls_back():
    result = review_labels("ru", state(labels={"ru": RU}), LanguageCode.FR)

    assert result == ("fallback", LanguageCode.FR, None)


def test_no_state_falls_back():
    assert review_labels("ru", None, LanguageCode.EN) == ("fallback", LanguageCode.EN, None)


@pytest.mark.parametrize("code,language", [("uk", LanguageCode.UK), ("en", LanguageCode.EN), ("fr", LanguageCode.FR),
                                           ("es", LanguageCode.ES)])
def test_a_ui_language_code_uses_its_own_strings_and_ignores_the_cache(code, language):
    cached = state(labels={code: RU}, label_digests={code: labels_digest()})

    assert review_labels(code, cached, LanguageCode.UK if code != "uk" else LanguageCode.EN) == ("ui", language, None)
