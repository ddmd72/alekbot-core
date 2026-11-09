"""Every fact domain has a UI label in each supported locale (fact profile RFC §3.2): Cabinet, the baseline sections
and the connector show the label, never the key."""
import pytest

from src.domain.fact_domains import FACT_DOMAINS, all_keys, label

_LOCALES = ("en", "uk", "fr", "es")


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("key", all_keys())
def test_every_key_has_a_label_in_every_locale(key, locale):
    entry = next(e for e in FACT_DOMAINS if e.key == key)
    text = entry.labels.get(locale, "")
    assert text.strip() == text and text, f"{key} has no {locale} label"
    assert label(key, locale) == text


@pytest.mark.parametrize("locale", _LOCALES)
def test_labels_are_distinct_within_a_locale(locale):
    labels = [e.labels[locale] for e in FACT_DOMAINS]
    assert len(set(labels)) == len(labels)


def test_no_entry_carries_a_locale_outside_the_supported_set():
    assert {loc for e in FACT_DOMAINS for loc in e.labels} == set(_LOCALES)
