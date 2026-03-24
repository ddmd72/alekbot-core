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
