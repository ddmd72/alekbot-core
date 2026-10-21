"""The `_Transcripts` read-me states the owner ruling of 2026-10-21 (USER_DRIVE_INDEXING_RFC §27.9.1, Δ241) in every
language: the bot overwrites every file there, the archive included; a version of one's own is a copy in another
folder; deleting an archived file makes the bot forget it."""
import pytest

from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

PHRASES = {
    "en": (en, ("overwrites every file here, in Archive/ too", "copy the file to another folder",
                "deleting a file there makes the bot forget it")),
    "uk": (uk, ("перезаписує кожен файл тут, і в Archive/ теж", "скопіюйте файл до іншої папки",
                "бот про нього забуде")),
    "fr": (fr, ("écrase chaque fichier ici, y compris dans Archive/", "copiez le fichier dans un autre dossier",
                "le bot l'oublie")),
    "es": (es, ("sobrescribe cada archivo de aquí, también en Archive/", "copia el archivo a otra carpeta",
                "el bot lo olvida")),
}
POINTS = ("overwritten, archive included", "copy to keep your own", "deleting an archived file forgets it")


@pytest.mark.parametrize("code", sorted(PHRASES))
@pytest.mark.parametrize("point", range(len(POINTS)), ids=POINTS)
def test_readme_states_the_overwrite_ruling(code, point):
    module, phrases = PHRASES[code]
    assert phrases[point] in module.UI_STRINGS[UIMessage.DRIVE_TRANSCRIPTS_README.value], (code, POINTS[point])
