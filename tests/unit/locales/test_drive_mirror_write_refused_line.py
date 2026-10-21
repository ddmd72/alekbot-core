"""The chat guard's refusal line (USER_DRIVE_INDEXING_RFC §27.9.8, Δ241): the folder is the bot's and its files are
overwritten, and a version of one's own is a copy in another folder; it never invites an edit in OneDrive. Each locale
keeps its register."""
import pytest

from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

PHRASES = {
    "en": (en, ("managed by the bot", "overwritten", "copy the file to another folder")),
    "uk": (uk, ("веде бот", "перезаписуються", "скопіюйте файл до іншої папки")),
    "fr": (fr, ("géré par le bot", "réécrits", "copiez le fichier dans un autre dossier")),
    "es": (es, ("la gestiona el bot", "se sobrescriben", "copia el archivo a otra carpeta")),
}


@pytest.mark.parametrize("code", sorted(PHRASES))
def test_refusal_line_states_the_overwrite_ruling(code):
    module, phrases = PHRASES[code]
    line = module.UI_STRINGS[UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value]
    for phrase in phrases:
        assert phrase in line, (code, phrase)
    assert "OneDrive" not in line
