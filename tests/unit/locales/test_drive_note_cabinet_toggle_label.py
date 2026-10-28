"""The drive-note receipt's "search is off" line names the Cabinet toggle exactly as the Cabinet labels it in that
language (`drive.index_title` in cabinet.html), so the owner finds the switch the line tells him to turn on."""
import re
from pathlib import Path

import pytest

from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

_CABINET = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _cabinet_label(lang: str) -> str:
    html = _CABINET.read_text(encoding="utf-8")
    start = re.search(rf"^\s+{lang}: \{{$", html, re.M)
    assert start is not None, lang
    label = re.compile(r'"drive\.index_title": "([^"]+)"').search(html, start.end())
    assert label is not None, lang
    return label.group(1)


@pytest.mark.parametrize("lang,module", [("uk", uk), ("en", en), ("fr", fr), ("es", es)])
def test_the_indexing_off_line_names_the_cabinet_toggle(lang, module):
    assert _cabinet_label(lang) in module.UI_STRINGS[UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF.value]
