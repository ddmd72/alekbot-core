"""`has_untrusted_wrapper`: did a drive/external wrapper (or an attached-file reminder) enter this text?
(EXTERNAL_TASK_QUEUE_RFC §5.3 taint rule; the look-alike pattern is `untrusted_text`'s own.)"""
import pytest

from src.domain.untrusted_text import (
    EXTERNAL_REMINDER,
    EXTERNAL_REMINDER_ATTACHED,
    REFERENCE_REMINDER_ATTACHED,
    has_untrusted_wrapper,
    wrap_untrusted,
)


@pytest.mark.parametrize("text", [
    "",
    "compare three community-garden irrigation kits for Shelbyville",
    "a < b and c > d, </div> and <b>bold</b>",
    '<system_note nonce="abc12345">a keyed note is in every history</system_note nonce="abc12345">',
])
def test_text_without_a_wrapper_is_clean(text):
    assert has_untrusted_wrapper(text) is False


@pytest.mark.parametrize("store", ["email", "chat", "workspace", "delivered", "drive"])
def test_a_wrapped_result_taints(store):
    wrapped = wrap_untrusted("hello", nonce="abcdef0123456789", store=store, ref="r", label="l")
    assert has_untrusted_wrapper(f"Tool note: {wrapped}") is True


@pytest.mark.parametrize("lookalike", [
    "<external_content nonce=x>",
    "</external_content nonce=x>",
    "< EXTERNAL_CONTENT",
    "＜external_content",                     # fullwidth opener
    "<ｅｘｔｅｒｎａｌ＿ｃｏｎｔｅｎｔ",             # fullwidth letters
    "<external​_content",                # a zero-width space inside the word
    "<drive_document nonce=x>",
    "﹤/drive_document",
])
def test_a_look_alike_of_either_untrusted_tag_taints(lookalike):
    assert has_untrusted_wrapper(f"before {lookalike} after") is True


@pytest.mark.parametrize("reminder", [REFERENCE_REMINDER_ATTACHED, EXTERNAL_REMINDER_ATTACHED, EXTERNAL_REMINDER])
def test_an_attached_file_reminder_taints(reminder):
    assert has_untrusted_wrapper(f"opened the file. {reminder}") is True
