"""The connector's keyed token bodies: the parser and the key-set and size checks (drive MCP connector concept §4.2,
§4.3)."""
import pytest

from src.domain.connector_texts import MAX_TOOL_DESCRIPTION_CHARS, ConnectorTexts, parse_keyed_sections

TOOL_BODY = """
### search_drive
Search the owner's drive by a few words.

A found invoice such as FA-0137 shows its folder and date.

### search_drive.query
The words to look for, e.g. "Tom's plan".
"""
RESULT_BODY = """### R_FIND
Answer from the cards: R&D/Tom's plan.docx is a card.
### R_EXCERPTS
Passages about 12 Oak Street are excerpts.
"""
TOOL_KEYS = frozenset({"search_drive", "search_drive.query"})
RESULT_KEYS = frozenset({"R_FIND", "R_EXCERPTS"})


# -- parse_keyed_sections ----------------------------------------------------------------------------------------

def test_each_header_gets_its_text_with_inner_newlines_kept_and_edge_blanks_dropped():
    assert parse_keyed_sections(TOOL_BODY) == {
        "search_drive": ("Search the owner's drive by a few words.\n\n"
                         "A found invoice such as FA-0137 shows its folder and date."),
        "search_drive.query": 'The words to look for, e.g. "Tom\'s plan".',
    }


def test_keys_keep_their_body_order_and_crlf_bodies_parse_the_same():
    body = RESULT_BODY.replace("\n", "\r\n")
    assert list(parse_keyed_sections(body)) == ["R_FIND", "R_EXCERPTS"]
    assert parse_keyed_sections(body) == parse_keyed_sections(RESULT_BODY)


def test_a_deeper_markdown_header_is_text_not_a_key():
    assert parse_keyed_sections("### R_FIND\n#### Tom's plan\nline") == {"R_FIND": "#### Tom's plan\nline"}


def test_text_before_the_first_header_is_refused():
    with pytest.raises(ValueError, match="before the first.*line 2.*12 Oak Street"):
        parse_keyed_sections("\n12 Oak Street\n### R_FIND\nFind.")


def test_a_duplicate_key_is_refused():
    with pytest.raises(ValueError, match="duplicate key 'R_FIND' on line 3"):
        parse_keyed_sections("### R_FIND\nFind.\n### R_FIND\nFind again.")


@pytest.mark.parametrize("header", ["###", "###   "])
def test_an_empty_key_is_refused(header):
    with pytest.raises(ValueError, match="empty key.*line 1"):
        parse_keyed_sections(f"{header}\nFind.")


@pytest.mark.parametrize("body", ["### R_FIND\n### R_EXCERPTS\nPassages.", "### R_FIND\nFind.\n### R_EXCERPTS\n\n  \n"])
def test_an_empty_text_is_refused(body):
    with pytest.raises(ValueError, match="empty text for key"):
        parse_keyed_sections(body)


# -- ConnectorTexts.from_bodies ------------------------------------------------------------------------------------

def test_from_bodies_holds_both_maps_read_only():
    texts = ConnectorTexts.from_bodies(TOOL_BODY, RESULT_BODY, TOOL_KEYS, RESULT_KEYS)
    assert texts.results["R_EXCERPTS"] == "Passages about 12 Oak Street are excerpts."
    assert set(texts.tools) == TOOL_KEYS
    with pytest.raises(TypeError):
        texts.tools["search_drive"] = "changed"  # type: ignore[index]


def test_the_constructor_copies_its_maps():
    source = {"R_FIND": "Find."}
    texts = ConnectorTexts(tools={}, results=source)
    source["R_FIND"] = "changed"
    assert texts.results["R_FIND"] == "Find."


def test_a_missing_key_is_named():
    with pytest.raises(ValueError, match="tool texts: missing keys search_drive.query$"):
        ConnectorTexts.from_bodies("### search_drive\nSearch.", RESULT_BODY, TOOL_KEYS, RESULT_KEYS)


def test_an_unknown_key_is_named():
    body = RESULT_BODY + "### R_PAGED\nPages.\n"
    with pytest.raises(ValueError, match="result texts: unknown keys R_PAGED$"):
        ConnectorTexts.from_bodies(TOOL_BODY, body, TOOL_KEYS, RESULT_KEYS)


def test_every_missing_and_unknown_key_of_both_bodies_is_named_together():
    tool_body = "### search_drive\nSearch.\n### open_file\nOpen."
    result_body = "### R_FIND\nFind.\n### R_PAGED\nPages.\n### R_SCAN\nScan."
    with pytest.raises(ValueError) as caught:
        ConnectorTexts.from_bodies(tool_body, result_body, TOOL_KEYS, RESULT_KEYS)
    assert str(caught.value) == ("tool texts: missing keys search_drive.query; tool texts: unknown keys open_file; "
                                 "result texts: missing keys R_EXCERPTS; result texts: unknown keys R_PAGED, R_SCAN")


def test_a_malformed_body_raises_before_the_key_check():
    with pytest.raises(ValueError, match="duplicate key"):
        ConnectorTexts.from_bodies(TOOL_BODY, RESULT_BODY + "### R_FIND\nAgain.", TOOL_KEYS, RESULT_KEYS)


def _tool_body(chars: int) -> str:
    return f"### search_drive\n{'x' * chars}\n### search_drive.query\nThe words."


def test_a_tool_text_at_the_limit_passes():
    texts = ConnectorTexts.from_bodies(_tool_body(MAX_TOOL_DESCRIPTION_CHARS), RESULT_BODY, TOOL_KEYS, RESULT_KEYS)
    assert len(texts.tools["search_drive"]) == MAX_TOOL_DESCRIPTION_CHARS == 4096


def test_a_tool_text_over_the_limit_is_refused_by_key():
    with pytest.raises(ValueError, match=r"over 4096 characters: search_drive \(4097 chars\)$"):
        ConnectorTexts.from_bodies(_tool_body(MAX_TOOL_DESCRIPTION_CHARS + 1), RESULT_BODY, TOOL_KEYS, RESULT_KEYS)


def test_the_limit_is_on_tool_texts_only():
    long_result = f"### R_FIND\n{'x' * (MAX_TOOL_DESCRIPTION_CHARS + 1)}\n### R_EXCERPTS\nPassages."
    texts = ConnectorTexts.from_bodies(TOOL_BODY, long_result, TOOL_KEYS, RESULT_KEYS)
    assert len(texts.results["R_FIND"]) == MAX_TOOL_DESCRIPTION_CHARS + 1
