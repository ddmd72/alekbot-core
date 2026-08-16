"""
Language domain types for multilingual support.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md

Adding a new language:
  1. Add entry to LanguageCode.
  2. Create src/locales/{code}.py (copy en.py structure).
  3. Register in FileLocalizationAdapter._REGISTRY.
  4. Add LANG_FIXED_{CODE} token to Firestore (migration script).
  Done — zero other changes.
"""
from enum import Enum
from typing import Optional


class LanguageCode(str, Enum):
    """Supported bot interface languages."""
    UK = "uk"
    EN = "en"
    FR = "fr"
    ES = "es"

    @classmethod
    def from_str(cls, value: str, default: Optional["LanguageCode"] = None) -> "LanguageCode":
        """Safe parser — never raises, falls back to default."""
        try:
            return cls(value.lower())
        except (ValueError, AttributeError):
            return default or cls.UK

    @classmethod
    def is_supported(cls, value: str) -> bool:
        return value in {m.value for m in cls}


def normalize_voice_languages(raw: object) -> list[str]:
    """Normalize spoken-language codes for voice transcription.

    Deliberately NOT validated against LanguageCode: that enum is the closed set of
    translated UI languages, while a speaker may use any language the recogniser knows
    (it has no "ru", and a multilingual household is the normal case). Only the shape is
    checked — which codes actually work is the provider's business.

    Order is preserved but carries no known meaning: the provider documents these as
    *possible* languages of the audio and states no precedence. Duplicates are dropped.

    Raises:
        ValueError: input is not a list, or a code is not a two-letter ISO-639-1 code.
    """
    if not isinstance(raw, list):
        raise ValueError("voice_languages must be a list")

    codes: list[str] = []
    for item in raw:
        code = str(item).strip().lower()
        if not (len(code) == 2 and code.isalpha()):
            raise ValueError(f"Invalid ISO-639-1 code: {item}")
        if code not in codes:
            codes.append(code)
    return codes


def resolve_lang_token_id(
    preferred_language: Optional[LanguageCode],
    agent_mirror: bool,
    system_default: LanguageCode,
) -> str:
    """
    Resolve Firestore token ID from user language settings.

    Token inventory (N languages + 1):
      LANG_MIRROR
      LANG_FIXED_EN
      LANG_FIXED_UK
      LANG_FIXED_FR
      LANG_FIXED_ES
      ... (one per LanguageCode)

    Pure function — no I/O, no imports beyond stdlib.
    """
    if agent_mirror:
        return "LANG_MIRROR"
    effective = preferred_language or system_default
    return f"LANG_FIXED_{effective.value.upper()}"
