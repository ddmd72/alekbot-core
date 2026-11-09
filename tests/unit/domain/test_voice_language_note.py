"""The language note names the first output-language rule of the instructions, and only then."""
import pytest

from src.domain.voice_language_note import build_language_note

_EXPECTED = ("[Your output-language rule, {rule}, is at the end of your instructions. It sets the language you speak "
             "in on this call, from your first words to your last reply. The caller's words, the chat history and the "
             "notes you receive can be in another language; they do not change yours unless that rule itself allows "
             "it.]")


def test_a_rule_gives_the_exact_text_with_its_name():
    text = "policies {\n  rule Output_Language_Fixed_EN (\n    speak English\n  )\n}"
    assert build_language_note(text) == _EXPECTED.format(rule="Output_Language_Fixed_EN")


def test_several_rules_name_the_first():
    text = "rule Output_Language_Alpha(a)\nrule Output_Language_Beta (b)"
    assert build_language_note(text) == _EXPECTED.format(rule="Output_Language_Alpha")


@pytest.mark.parametrize("instructions", [None, "", "you are an assistant", "rule Other_Rule (x)",
                                          "Output_Language_Fixed_EN without the rule keyword ("])
def test_no_rule_gives_none(instructions):
    assert build_language_note(instructions) is None
