"""The Cabinet drive disclosure's Search v2 sentence (USER_DRIVE_INDEXING_RFC §25.6, D26 revised): what goes to an AI
model provider to describe the files, naming OpenAI with store=false and its abuse-monitoring retention, the Gemini
fallback and the Claude switch, in every Cabinet language. No disclosure version and no acceptance step."""
import pytest

from .test_cabinet_drive_index_strings import LANGS, _dicts

_SENTENCE = {
    "en": ("To describe your files for search, the beginning and end of every readable file, the text of your "
           "documents passage by passage, and a preview of every picture go to an AI model provider: OpenAI by "
           "default (asked not to store them, store=false; the provider may keep them for a limited time for abuse "
           "monitoring), Google Gemini when OpenAI is unavailable, or Anthropic if you chose Claude."),
    "uk": "типово OpenAI (з проханням не зберігати їх, store=false;",
    "fr": "OpenAI par défaut (avec la demande de ne pas les conserver, store=false ;",
    "es": "OpenAI de forma predeterminada (con la petición de no guardarlos, store=false;",
}
_FALLBACK = {"en": "Google Gemini when OpenAI is unavailable", "uk": "Google Gemini, коли OpenAI недоступний",
             "fr": "Google Gemini quand OpenAI est indisponible", "es": "Google Gemini cuando OpenAI no está disponible"}
_CLAUDE = {"en": "Anthropic if you chose Claude", "uk": "Anthropic, якщо ти обрав Claude",
           "fr": "Anthropic si tu as choisi Claude", "es": "Anthropic si elegiste Claude"}


@pytest.mark.parametrize("lang", LANGS)
def test_the_disclosure_names_the_providers_of_the_descriptions(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    assert _SENTENCE[lang] in text and _FALLBACK[lang] in text and _CLAUDE[lang] in text


@pytest.mark.parametrize("lang", LANGS)
def test_no_old_two_provider_parenthesis(lang):
    _, dicts = _dicts()
    text = dicts[lang]["drive.index_disclosure"]
    assert "(Anthropic or OpenAI)" not in text and "(Anthropic або OpenAI)" not in text
    assert "(Anthropic ou OpenAI)" not in text and "(Anthropic u OpenAI)" not in text
