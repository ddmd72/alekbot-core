"""The Cabinet's `drive.mirror_clause` states the owner ruling of 2026-10-21 (USER_DRIVE_INDEXING_RFC Δ241) in every
language, in the Cabinet's register (en you, uk ти, fr tu, es tú): the files are the bot's and overwritten, a copy
elsewhere keeps one's own version, and deleting an archived file makes the bot forget it."""
import pytest

from .test_cabinet_drive_index_strings import LANGS, _dicts

pytestmark = pytest.mark.datafile

PHRASES = {
    "en": ("overwritten", "copy it elsewhere", "Deleting an archived file makes the bot forget it"),
    "uk": ("перезаписуються", "скопіюй файл", "Якщо ти видалиш файл з архіву, бот про нього забуде"),
    "fr": ("réécrits", "copie-la ailleurs", "Si tu supprimes un fichier archivé, le bot l'oublie"),
    "es": ("sobrescribe", "cópiala en otro sitio", "Si borras un archivo archivado, el bot lo olvida"),
}


@pytest.mark.parametrize("lang", LANGS)
def test_mirror_clause_states_the_overwrite_ruling(lang):
    _, dicts = _dicts()
    clause = dicts[lang]["drive.mirror_clause"]
    for phrase in PHRASES[lang]:
        assert phrase in clause, (lang, phrase)
