"""The proactive notice about files the bot cannot open is a fixed localized UI string (USER_DRIVE_INDEXING_RFC §26.2,
D33, Δ53): it carries only the count and the fix, in every locale, so no file name can enter it."""
import string

import pytest

from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk


def _fields(template):
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


@pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_the_notice_carries_only_the_count(mod):
    notice = mod.UI_STRINGS[UIMessage.DRIVE_INACCESSIBLE_NOTICE.value]
    assert _fields(notice) == {"count"}
    assert notice.format(count=3).count("3") == 1


@pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_the_list_title_has_no_placeholder(mod):
    assert _fields(mod.UI_STRINGS[UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE.value]) == set()


def test_the_english_notice_says_the_fix():
    text = en.UI_STRINGS[UIMessage.DRIVE_INACCESSIBLE_NOTICE.value]
    assert "make a copy of each file in the same folder" in text and "sharing settings" in text
