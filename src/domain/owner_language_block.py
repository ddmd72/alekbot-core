"""The `owner_language` block of a prompt: the owner's language as data, for a prompt whose output is for the owner but
whose input is in other languages (a call kept as a file, VOICE_CALL_LIVE_TEXT_RFC §4.11). The rule that uses it lives
in the prompt token; this block only names the language, by its ISO 639-1 code."""
from .language import LanguageCode

OWNER_LANGUAGE_BLOCK = "owner_language"


def render_owner_language_block(language: LanguageCode) -> str:
    """The block a prompt carries after its template: `owner_language { iso_639_1: "<code>" }`."""
    return f'{OWNER_LANGUAGE_BLOCK} {{\n    iso_639_1: "{language.value}"\n}}'
