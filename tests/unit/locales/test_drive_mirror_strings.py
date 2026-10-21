"""The transcript mirror's localized text (USER_DRIVE_INDEXING_RFC §27.9.1, §27.9.8, §27.9.10; D49; plan Task 21C): the
read-me of `_Transcripts`, the file header's labels and managed line, the four stub sentences and the Step 1 guard's line
exist in every locale, format with the values the renderer gives them, and no folder name is localized (D49: the names
are fixed in every language)."""
import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.drive_mirror import ARCHIVE_NAME, LONG_PATHS_NAME
from src.domain.drive_mirror_render import MirrorStrings, readme_bytes
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk
from src.services.drive_indexing_service import _mirror_strings
from src.services.localization_service import LocalizationService

MIRROR_KEYS = (
    UIMessage.DRIVE_TRANSCRIPTS_README, UIMessage.DRIVE_TRANSCRIPT_OPEN_RECORDING,
    UIMessage.DRIVE_TRANSCRIPT_MANAGED_LINE, UIMessage.DRIVE_TRANSCRIPT_DURATION_MINUTES,
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_ONE, UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_MANY,
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_SPEECH, UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LONG,
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LARGE, UIMessage.DRIVE_TRANSCRIPT_STUB_NO_AUDIO,
    UIMessage.DRIVE_MIRROR_WRITE_REFUSED,
)
LOCALES = {"uk": uk, "en": en, "fr": fr, "es": es}


@pytest.mark.parametrize("code", sorted(LOCALES))
def test_every_locale_has_every_key(code):
    strings = LOCALES[code].UI_STRINGS
    for key in MIRROR_KEYS:
        assert strings.get(key.value, "").strip(), (code, key)
    built = _mirror_strings(LocalizationService(FileLocalizationAdapter()), LanguageCode(code))
    assert isinstance(built, MirrorStrings) and all(getattr(built, f) for f in MirrorStrings.__dataclass_fields__)


@pytest.mark.parametrize("code", sorted(LOCALES))
def test_readme_text_in_every_locale(code):
    """D49: managed by the bot, files may be rewritten, do not edit, transcripts of deleted recordings in `Archive/` —
    the archive named by its fixed name in every language."""
    text = LOCALES[code].UI_STRINGS[UIMessage.DRIVE_TRANSCRIPTS_README.value]
    assert f"{ARCHIVE_NAME}/" in text
    assert readme_bytes(_mirror_strings(LocalizationService(FileLocalizationAdapter()), LanguageCode(code))) \
        == (text.strip("\n") + "\n").encode("utf-8")
    if code == "en":
        assert "Managed by the bot" in text and "may be rewritten" in text and "do not edit" in text


@pytest.mark.parametrize("code", sorted(LOCALES))
def test_counts_and_stubs_format(code):
    built = _mirror_strings(LocalizationService(FileLocalizationAdapter()), LanguageCode(code))
    for template in (built.duration_minutes, built.speakers_one, built.speakers_many):
        assert "7" in template.format(n=7)
    for template in (built.stub_no_speech, built.stub_too_long, built.stub_too_large, built.stub_no_audio):
        assert "{" not in template.format(duration="12:34")
    assert "12:34" in built.stub_no_speech.format(duration="12:34")
    assert "12:34" in built.stub_too_long.format(duration="12:34")


def test_no_folder_name_is_localized():
    """No UI string names the mirror's folders: `_Transcripts`, `Archive` and `Long paths` are fixed (D49)."""
    names = {m.name for m in UIMessage}
    assert not [n for n in names if "TRANSCRIPT" in n and ("FOLDER" in n or "ARCHIVE" in n)]
    for module in LOCALES.values():
        for key in MIRROR_KEYS:
            assert LONG_PATHS_NAME not in module.UI_STRINGS[key.value]
