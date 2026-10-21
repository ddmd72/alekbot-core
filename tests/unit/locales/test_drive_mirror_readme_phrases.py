"""The `_Transcripts` read-me states all four D49 points in every language (USER_DRIVE_INDEXING_RFC §27.9.1; Task 21C fix
round 1, review Minor 2): managed by the bot, its files may be rewritten, do not edit them, and transcripts of deleted
recordings are in `Archive/` (the fixed name). One phrase per point and locale."""
import pytest

from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

PHRASES = {
    "en": (en, ("Managed by the bot", "may be rewritten", "do not edit", "deleted recordings are in Archive/")),
    "uk": (uk, ("веде бот", "можуть бути переписані", "не редагуйте", "видалених записів лежать в Archive/")),
    "fr": (fr, ("Géré par le bot", "peuvent être réécrits", "ne les modifiez pas",
                "enregistrements supprimés sont dans Archive/")),
    "es": (es, ("Gestionado por el bot", "pueden reescribirse", "no los edites",
                "grabaciones eliminadas están en Archive/")),
}
POINTS = ("managed by the bot", "files may be rewritten", "do not edit", "deleted recordings in Archive/")


@pytest.mark.parametrize("code", sorted(PHRASES))
@pytest.mark.parametrize("point", range(len(POINTS)), ids=POINTS)
def test_readme_states_each_d49_point(code, point):
    module, phrases = PHRASES[code]
    text = module.UI_STRINGS[UIMessage.DRIVE_TRANSCRIPTS_README.value]
    assert phrases[point] in text, (code, POINTS[point])
