"""The interpreter call's language pair: the tool-call parser, the declaration the relay offers, and the texts."""
import json

import pytest

from src.domain.voice_language_pair import (
    LANGUAGE_NAMES,
    PAIR_TOOL_ALREADY,
    PAIR_TOOL_BAD,
    PAIR_TOOL_REFUSED,
    SET_LANGUAGE_PAIR,
    LanguagePair,
    language_pair_tool_declaration,
    parse_language_pair,
)

_CODES = ["ar", "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "he", "hi", "hr", "hu", "it", "ja", "ko", "lt", "lv", "nl", "no", "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv", "tr", "uk", "zh"]


def _args(**fields) -> str:
    return json.dumps(fields)


class TestParse:
    def test_two_known_different_codes_make_the_pair(self):
        pair = parse_language_pair(_args(caller_language="uk", other_language="fr"))
        assert pair == LanguagePair(caller="uk", other="fr")
        assert (pair.caller_name, pair.other_name) == ("Ukrainian", "French")

    @pytest.mark.parametrize("arguments", [
        pytest.param(_args(caller_language="uk", other_language="xx"), id="unknown-other"),
        pytest.param(_args(caller_language="UK", other_language="fr"), id="code-case-differs"),
        pytest.param(_args(caller_language="es", other_language="es"), id="same-code"),
        pytest.param(_args(caller_language="uk"), id="missing-other"),
        pytest.param(_args(other_language="fr"), id="missing-caller"),
        pytest.param(_args(caller_language=1, other_language="fr"), id="not-a-string"),
        pytest.param("{not json", id="bad-json"),
        pytest.param(json.dumps(["uk", "fr"]), id="not-an-object"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
    ])
    def test_anything_else_is_no_pair(self, arguments):
        assert parse_language_pair(arguments) is None

    def test_every_code_of_the_closed_set_parses(self):
        for caller in _CODES:
            other = "en" if caller != "en" else "uk"
            assert parse_language_pair(_args(caller_language=caller, other_language=other)) is not None


class TestDeclaration:
    def test_shape_is_the_neutral_function_shape_with_the_closed_set(self):
        decl = language_pair_tool_declaration()
        assert decl["name"] == SET_LANGUAGE_PAIR == "set_language_pair"
        assert "type" not in decl  # the adapter wraps it as a function
        params = decl["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["caller_language", "other_language"]
        for key in ("caller_language", "other_language"):
            assert params["properties"][key]["type"] == "string"
            assert params["properties"][key]["enum"] == _CODES
        assert params["properties"]["caller_language"]["description"] == "ISO 639-1 code of the caller's language."
        assert params["properties"]["other_language"]["description"] == (
            "ISO 639-1 code of the other person's language.")

    def test_description_says_when_it_is_accepted_and_what_follows(self):
        description = language_pair_tool_declaration()["description"]
        assert description.startswith("Sets the two languages of this call.")
        assert "on words meant for translation it is refused and nothing changes" in description
        assert description.endswith("Words of the caller that set the pair are never translated.")

    def test_the_closed_set_is_the_names_map_in_order(self):
        assert list(LANGUAGE_NAMES) == _CODES
        assert LANGUAGE_NAMES["ja"] == "Japanese" and LANGUAGE_NAMES["nl"] == "Dutch"


class TestTexts:
    PAIR = LanguagePair(caller="de", other="pt")

    @pytest.mark.parametrize("text", ["tool_ok", "block", "note"])
    def test_each_text_names_both_languages_by_name(self, text):
        rendered = getattr(self.PAIR, text)()
        assert "German" in rendered and "Portuguese" in rendered
        assert "{" not in rendered and " de " not in rendered

    def test_ok_is_a_tool_note_and_confirms_in_the_callers_language(self):
        ok = self.PAIR.tool_ok()
        assert ok.startswith("Tool note: pair set: the caller speaks German, the other person speaks Portuguese.")
        assert ok.endswith("one short sentence in German confirming the pair, said once.")

    def test_block_and_note_give_the_direction_both_ways(self):
        assert "words in German you say in Portuguese; words in any other language you say in German" in (
            self.PAIR.block())
        assert self.PAIR.block().startswith("Language pair for this call.")
        note = self.PAIR.note()
        assert note.startswith("[") and note.endswith("]")
        assert "If these words are in German, say them in Portuguese; if they are in any other language, say them in " \
               "German." in note

    def test_refusals_are_tool_errors(self):
        assert PAIR_TOOL_REFUSED.startswith("Tool error: ") and PAIR_TOOL_REFUSED.endswith("Nothing changed.")
        assert PAIR_TOOL_BAD == "Tool error: unknown language code. Nothing changed."


class TestRefusalTexts:
    """Each refusal says what is true in the case that produces it (text review)."""

    def test_already_set_names_the_earlier_acceptance_not_translation(self):
        assert PAIR_TOOL_ALREADY == "Tool error: the pair was already set from these words. Nothing changed."
        assert "translation" not in PAIR_TOOL_ALREADY

    def test_bad_codes_asks_for_two_different_codes_from_the_list(self):
        assert PAIR_TOOL_BAD == (
            "Tool error: the two languages must be two different codes from the list. Nothing changed.")

    def test_the_three_refusals_are_distinct_tool_errors(self):
        texts = {PAIR_TOOL_REFUSED, PAIR_TOOL_ALREADY, PAIR_TOOL_BAD}
        assert len(texts) == 3
        assert all(t.startswith("Tool error: ") and t.endswith("Nothing changed.") for t in texts)
