"""
FileLocalizationAdapter — file-backed implementation of LocalizationPort.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §9

Only place in the system that imports locale modules directly.

To add a language:
  1. src/locales/{code}.py
  2. LanguageCode.{CODE} in domain/language.py
  3. Entry in _REGISTRY below
  Done.
"""
from typing import List

from ..ports.localization_port import LocalizationPort
from ..domain.language import LanguageCode
from ..domain.ui_messages import StatusType, UIMessage
from ..locales import uk, en, fr, es


class FileLocalizationAdapter(LocalizationPort):
    """File-backed localization. Single point of locale module imports."""

    _REGISTRY = {
        LanguageCode.UK: uk,
        LanguageCode.EN: en,
        LanguageCode.FR: fr,
        LanguageCode.ES: es,
    }
    _DEFAULT = en  # matches system default language

    def _module(self, lang: LanguageCode):
        return self._REGISTRY.get(lang, self._DEFAULT)

    def get_status_phrases(self, lang: LanguageCode, status: StatusType) -> List[str]:
        return self._module(lang).get_message(status)

    def get_entertainment_intros(self, lang: LanguageCode) -> List[str]:
        return self._module(lang).ENTERTAINMENT_INTROS

    def get_file_prompt(self, lang: LanguageCode, mime_type: str) -> str:
        mod = self._module(lang)
        if "image" in mime_type:
            return mod.FILE_FALLBACK_IMAGE
        if "video" in mime_type:
            return mod.FILE_FALLBACK_VIDEO
        if "pdf" in mime_type:
            return mod.FILE_FALLBACK_PDF
        if "document" in mime_type or "text/" in mime_type:
            return mod.FILE_FALLBACK_DOCUMENT
        return mod.FILE_FALLBACK_GENERIC

    def get_ui_string(self, lang: LanguageCode, message: UIMessage) -> str:
        return self._module(lang).UI_STRINGS[message.value]

    def get_ui_string_variants(self, message: UIMessage) -> List[str]:
        variants: List[str] = []
        for mod in self._REGISTRY.values():
            text = mod.UI_STRINGS[message.value]
            if text not in variants:
                variants.append(text)
        return variants
