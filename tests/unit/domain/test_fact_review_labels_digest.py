"""Facts S4 · T7b (R44-4): the digest of the canonical label texts, and the rule that a cached set written for other texts is
absent (`current_labels`)."""
import hashlib
import json
import re

from src.domain import fact_review_language as language
from src.domain.fact_review_language import CANONICAL_LABELS, current_labels, label_source, labels_digest
from src.domain.fact_review_state import FactReviewState

RU = {key: f"ру {key.split('_')[-1]}" for key in CANONICAL_LABELS}


def test_the_digest_is_a_stable_hash_of_the_canonical_texts():
    assert re.fullmatch(r"[0-9a-f]{16}", labels_digest())
    assert labels_digest() == labels_digest()
    canonical = json.dumps(dict(sorted(CANONICAL_LABELS.items())), ensure_ascii=False, separators=(",", ":"))
    assert labels_digest() == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def test_the_digest_changes_when_a_canonical_text_changes(monkeypatch):
    before = labels_digest()
    changed = dict(CANONICAL_LABELS)
    changed[next(iter(changed))] = "Is this still so?"
    monkeypatch.setattr(language, "CANONICAL_LABELS", changed)

    assert labels_digest() != before


def test_a_set_under_the_current_digest_is_current():
    state = FactReviewState(account_id="acc", labels={"ru": RU}, label_digests={"ru": labels_digest()})

    assert dict(current_labels(state, "ru") or {}) == RU


def test_a_set_under_another_digest_or_none_is_absent():
    stale = FactReviewState(account_id="acc", labels={"ru": RU}, label_digests={"ru": "0123456789abcdef"})
    undigested = FactReviewState(account_id="acc", labels={"ru": RU})

    assert current_labels(stale, "ru") is None
    assert current_labels(undigested, "ru") is None
    assert label_source("ru", current_labels(stale, "ru")) == "fallback"


def test_no_state_or_no_set_is_absent():
    assert current_labels(None, "ru") is None
    assert current_labels(FactReviewState(account_id="acc", label_digests={"ru": labels_digest()}), "ru") is None
    other = FactReviewState(account_id="acc", labels={"pl": RU}, label_digests={"pl": labels_digest()})
    assert current_labels(other, "ru") is None
