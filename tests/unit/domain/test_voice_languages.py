"""Spoken-language codes for voice transcription.

Separate from LanguageCode by design: that enum is the closed set of translated UI
languages (uk/en/fr/es) and has no "ru" — it cannot express what a household actually
speaks. Only the shape is validated here; which codes a recogniser supports is the
provider's business, not the domain's.
"""
from __future__ import annotations

import pytest

from src.domain.language import LanguageCode, normalize_voice_languages


class TestNormalizeVoiceLanguages:

    def test_order_is_preserved_first_is_primary(self):
        assert normalize_voice_languages(["ru", "uk", "en"]) == ["ru", "uk", "en"]

    def test_accepts_a_language_the_ui_does_not_support(self):
        """The whole reason this is not LanguageCode."""
        assert "ru" not in {m.value for m in LanguageCode}
        assert normalize_voice_languages(["ru"]) == ["ru"]

    def test_five_languages_at_once(self):
        codes = normalize_voice_languages(["en", "fr", "ru", "uk", "es"])
        assert codes == ["en", "fr", "ru", "uk", "es"]

    def test_codes_are_lowercased_and_trimmed(self):
        assert normalize_voice_languages([" RU ", "Uk"]) == ["ru", "uk"]

    def test_duplicates_are_dropped_keeping_first_position(self):
        assert normalize_voice_languages(["en", "ru", "en"]) == ["en", "ru"]

    def test_empty_list_is_allowed_and_means_auto_detect(self):
        assert normalize_voice_languages([]) == []

    def test_non_list_is_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            normalize_voice_languages("ru")

    def test_three_letter_code_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid ISO-639-1 code"):
            normalize_voice_languages(["rus"])

    def test_locale_form_is_rejected(self):
        """ISO-639-1 only — `ru_RU` is a dictation locale, not a language code."""
        with pytest.raises(ValueError, match="Invalid ISO-639-1 code"):
            normalize_voice_languages(["ru_RU"])

    def test_digits_are_rejected(self):
        with pytest.raises(ValueError, match="Invalid ISO-639-1 code"):
            normalize_voice_languages(["r1"])

    def test_empty_string_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid ISO-639-1 code"):
            normalize_voice_languages([""])
