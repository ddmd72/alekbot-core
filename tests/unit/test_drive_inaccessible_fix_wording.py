"""The fix every user-facing text gives for a file the bot cannot open (USER_DRIVE_INDEXING_RFC §26.10 Δ75, arbiter
2026-10-14): copy each file into a new folder the user creates inside the bot's folder. A copy in the same folder
inherits that folder's permissions, so it stays closed when the whole folder lacks the app's entry."""
import pytest

from src.domain.drive_search_texts import ACCESS_LINE_FILE, ACCESS_LINE_FOLDER
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

FIX = {
    en: "copy each file into a new folder you create inside the bot's folder",
    uk: "скопіюйте кожен файл у нову папку, яку ви створите всередині папки бота",
    fr: "copie chaque fichier dans un nouveau dossier que tu crées dans le dossier du bot",
    es: "copia cada archivo en una carpeta nueva que crees dentro de la carpeta del bot",
}
SAME_FOLDER = ("same folder", "тій самій папці", "même dossier", "misma carpeta")


@pytest.mark.parametrize("mod", [en, uk, fr, es], ids=["en", "uk", "fr", "es"])
def test_the_notice_says_a_new_folder_inside_the_bot_s_folder(mod):
    notice = mod.UI_STRINGS[UIMessage.DRIVE_INACCESSIBLE_NOTICE.value]
    assert FIX[mod] in notice
    assert not any(words in notice for words in SAME_FOLDER)


def test_the_card_lines_say_a_folder_you_create():
    assert "copy the file into a new folder you create inside the bot's folder" in ACCESS_LINE_FILE
    assert "copy the files into a new folder you create inside the bot's folder" in ACCESS_LINE_FOLDER


def test_the_skill_tells_smart_the_same_fix():
    from src.domain import drive_answer_texts as t
    body = t.R_NO_ACCESS   # the bot-cannot-open reading line (DRIVE_SPECIALIST_RFC §5.7, §7.3)
    assert "copy each file into a new folder they create inside the bot's folder" in body
