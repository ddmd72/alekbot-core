"""Unit tests for the language domain types (multilingual support).

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md
Covers LanguageCode parsing/validation and the pure resolve_lang_token_id function.
"""

import pytest

from src.domain.language import LanguageCode, resolve_lang_token_id


class TestLanguageCodeFromStr:
    """Safe parser — never raises."""

    @pytest.mark.parametrize("value,expected", [
        ("uk", LanguageCode.UK),
        ("en", LanguageCode.EN),
        ("fr", LanguageCode.FR),
        ("es", LanguageCode.ES),
        ("EN", LanguageCode.EN),   # case-insensitive
        ("Es", LanguageCode.ES),
    ])
    def test_valid_values(self, value, expected):
        assert LanguageCode.from_str(value) == expected

    def test_unknown_value_falls_back_to_uk_by_default(self):
        assert LanguageCode.from_str("de") == LanguageCode.UK

    def test_unknown_value_uses_provided_default(self):
        assert LanguageCode.from_str("de", default=LanguageCode.EN) == LanguageCode.EN

    def test_none_value_falls_back_to_uk(self):
        # .lower() on None raises AttributeError → caught → default
        assert LanguageCode.from_str(None) == LanguageCode.UK

    def test_none_value_uses_provided_default(self):
        assert LanguageCode.from_str(None, default=LanguageCode.FR) == LanguageCode.FR


class TestLanguageCodeIsSupported:
    @pytest.mark.parametrize("value", ["uk", "en", "fr", "es"])
    def test_supported(self, value):
        assert LanguageCode.is_supported(value) is True

    @pytest.mark.parametrize("value", ["de", "EN", "", "english"])
    def test_unsupported(self, value):
        # Note: is_supported is case-sensitive on the raw value.
        assert LanguageCode.is_supported(value) is False


class TestResolveLangTokenId:
    """Pure resolution from user language settings to a Firestore token ID."""

    def test_mirror_mode_wins_over_everything(self):
        result = resolve_lang_token_id(
            preferred_language=LanguageCode.FR,
            agent_mirror=True,
            system_default=LanguageCode.EN,
        )
        assert result == "LANG_MIRROR"

    def test_fixed_uses_preferred_language(self):
        result = resolve_lang_token_id(
            preferred_language=LanguageCode.FR,
            agent_mirror=False,
            system_default=LanguageCode.EN,
        )
        assert result == "LANG_FIXED_FR"

    def test_fixed_falls_back_to_system_default_when_no_preference(self):
        result = resolve_lang_token_id(
            preferred_language=None,
            agent_mirror=False,
            system_default=LanguageCode.ES,
        )
        assert result == "LANG_FIXED_ES"
