"""The drive-index disclosure says who computes the search vectors (review E minor, USER_DRIVE_INDEXING_RFC §6,
§24.6): the names and the extracted text are sent to Google (Gemini) to be turned into search vectors, in every
Cabinet language."""
import pytest

from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts

_SENTENCE = {
    "en": "Google (Gemini) turns the file names and the text into these search vectors.",
    "uk": "Ці пошукові вектори з назв файлів і тексту обчислює Google (Gemini).",
    "fr": "Google (Gemini) transforme les noms des fichiers et le texte en ces vecteurs de recherche.",
    "es": "Google (Gemini) convierte los nombres de los archivos y el texto en estos vectores de búsqueda.",
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_disclosure_names_google_gemini_for_the_search_vectors(lang):
    _, dicts = _dicts()
    assert _SENTENCE[lang] in dicts[lang]["drive.index_disclosure"]
