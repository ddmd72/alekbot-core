"""The four tokens that tell the pass, the consolidator and Smart about translated quotes, on the rendered prompts."""
import re

from src.agents.consolidation_agent import _TRANSLATIONS_RULE
from src.domain.fact_review_frame import reply_part, review_frame
from src.domain.fact_review_quote_translation import MAX_TRANSLATION_CHARS
from tests.support import prompt_vocabulary as pv
from tests.support.prompt_render import render_static


def _body(token_id):
    return pv.ser.token_from_file(pv.token_path(token_id).read_text(encoding="utf-8"))["content"]


def test_the_final_report_names_translations_and_when_to_give_them():
    lifecycle = _body("CONSOLIDATION_LIFECYCLE")
    assert '\\"translations\\": …' in lifecycle
    assert "LANGUAGE or TRANSLATIONS section" in lifecycle


def test_the_echo_exception_says_the_quote_may_be_translated():
    line = next(l for l in _body("CONSOLIDATION_POLICIES").split("\n") if l.strip().startswith("memory_review_reply:"))
    assert "in the message's language — the quote may be the stored fact translated, and it is the same fact" in line


def test_smart_reads_the_quote_as_in_the_messages_language():
    assert "(in the message's language)" in _body("PROTOCOL_MEMORY_REVIEW_MARKERS")


def _case():
    """The case's lines (braces counted) and whether it sits inside `example_3b_memory_review_reply`."""
    text = render_static("consolidation")
    start = text.index("case_4_translated_quote: {")
    inside = text.index("example_3b_memory_review_reply") < start < text.index("example_4_merge_car_details")
    depth, out = 0, []
    for line in text[start:].split("\n"):
        out.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return "\n".join(out), inside


def test_the_translated_quote_case_quotes_in_ukrainian_and_leaves_the_fact_untouched():
    from src.locales import uk  # the uk UI strings

    case, inside = _case()
    assert inside
    lines = [l.strip() for l in case.split("\n")]
    frame_line = next(l for l in lines if l.startswith("USER: [System: memory review"))
    assert frame_line == f"USER: {review_frame('rv-5d3e')}"
    reply_line = next(l for l in lines if l.startswith("USER: [Reply to memory review"))
    assert reply_line.startswith("USER: " + reply_part("rv-5d3e", "")[:-1] + " ")
    assert not re.search("[А-Яа-яІіЇїЄє]", reply_line)  # the owner wrote in English; only the em dash is not ASCII
    question = next(l for l in lines if l.startswith("ASSISTANT: ") and "Це досі правда?" in l)
    assert re.search("[А-Яа-яІіЇїЄє]", question)
    texts = uk.UI_STRINGS
    for key in ("memory_review_question", "memory_option_still_true", "memory_option_ended", "memory_option_never",
                "memory_option_dont_know"):
        assert texts[key] in question
    assert "leave gym-88 untouched" in case
    assert "update_fact(" not in case and "create_fact(" not in case


def test_the_request_rule_names_the_bound_and_the_names_clause():
    assert str(MAX_TRANSLATION_CHARS) in _TRANSLATIONS_RULE
    assert "as that language usually spells them" in _TRANSLATIONS_RULE
