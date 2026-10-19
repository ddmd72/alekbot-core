import pytest
from pydantic import ValidationError

from src.domain.memory_fact import (
    MAX_FACT_CHARS,
    MAX_FACTS_PER_DRAFT,
    MAX_SOURCE_CHARS,
    MemoryFactDraft,
    ProposedFact,
    confirmed_reply,
    render_preview_file,
    render_confirmed_note,
    save_command,
)


def _fact(text: str = "User's passport expires on 2031-03-04.", source: str = "passport.pdf") -> ProposedFact:
    return ProposedFact(text=text, source=source)


def test_fact_is_stripped():
    fact = _fact("  User was born in 1980.  ", "  scan.jpg ")
    assert (fact.text, fact.source) == ("User was born in 1980.", "scan.jpg")


@pytest.mark.parametrize("text", ["", "   ", "line one\nline two", "x" * (MAX_FACT_CHARS + 1)])
def test_bad_fact_text_is_rejected(text):
    with pytest.raises(ValidationError):
        _fact(text=text)


@pytest.mark.parametrize("source", ["", "a\nb", "s" * (MAX_SOURCE_CHARS + 1)])
def test_bad_source_is_rejected(source):
    with pytest.raises(ValidationError):
        _fact(source=source)


def test_limits_are_inclusive():
    _fact(text="x" * MAX_FACT_CHARS, source="s" * MAX_SOURCE_CHARS)
    MemoryFactDraft(facts=[_fact()] * MAX_FACTS_PER_DRAFT)


def test_empty_draft_is_rejected():
    with pytest.raises(ValidationError):
        MemoryFactDraft(facts=[])


def test_oversized_draft_is_rejected():
    with pytest.raises(ValidationError, match="split"):
        MemoryFactDraft(facts=[_fact()] * (MAX_FACTS_PER_DRAFT + 1))


def test_save_command():
    assert save_command("a1b2") == "$memory save a1b2"


def test_max_facts_per_draft_is_twenty():
    assert MAX_FACTS_PER_DRAFT == 20


def test_confirmed_note_frame_and_fact_lines_without_per_fact_sources():
    draft = MemoryFactDraft(facts=[_fact("User has a daughter, Ana.", "family.pdf"), _fact()])

    frame, *lines = render_confirmed_note(draft).split("\n")

    assert frame == (
        "[System: the owner reviewed and confirmed these facts about their life and the people in it, "
        "read from: family.pdf, passport.pdf. Background consolidation adds them to memory; "
        "search_memory cannot find them before that.]"
    )
    assert lines == ["- User has a daughter, Ana.", "- User's passport expires on 2031-03-04."]


def test_confirmed_note_sources_are_unique_in_first_seen_order():
    draft = MemoryFactDraft(facts=[_fact("a", "b.pdf"), _fact("b", "a.pdf"), _fact("c", "b.pdf")])

    frame = render_confirmed_note(draft).split("\n")[0]

    assert "read from: b.pdf, a.pdf. " in frame


def test_origin_session_id_defaults_to_none():
    assert MemoryFactDraft(facts=[_fact()]).origin_session_id is None


def test_preview_file_is_numbered_blocks_separated_by_a_blank_line():
    draft = MemoryFactDraft(facts=[_fact("User has a daughter, Ana.", "family.pdf"), _fact()])

    assert render_preview_file(draft) == (
        "1. User has a daughter, Ana.\n   source: family.pdf\n"
        "\n"
        "2. User's passport expires on 2031-03-04.\n   source: passport.pdf\n"
    )


def test_preview_file_has_no_truncation_at_the_maximum():
    draft = MemoryFactDraft(facts=[_fact("x" * MAX_FACT_CHARS, "s" * MAX_SOURCE_CHARS)] * MAX_FACTS_PER_DRAFT)

    out = render_preview_file(draft)

    assert out.count("source: ") == MAX_FACTS_PER_DRAFT
    assert out.count("x" * MAX_FACT_CHARS) == MAX_FACTS_PER_DRAFT


def test_confirmed_reply():
    assert confirmed_reply(3) == "Submitted 3 confirmed facts to memory."
