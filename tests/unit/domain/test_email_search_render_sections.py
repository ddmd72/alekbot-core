"""The result sections of email search v2 (email index v2 §5.4; slice 8, D4): the text split by who wrote it, so the
formatter wraps only mail text. The sections joined by new lines are the text; mail sections hold only card and fold
lines; the heads, the absent, count and recard lines are the tool's; `shown` are the cards on a line of their own, in
order. Invented cast only."""
from datetime import date

from src.domain.email_card_text import render_result_line
from src.domain.email_search_fold import fold
from src.domain.email_search_merge import GROUP_ABOUT, GROUP_MEANING, GROUP_TRANSLATED, Candidate, RankedResult
from src.domain.email_search_render import SECTION_MAIL, SECTION_TOOL, render_list, render_ranked
from src.domain.email_search_texts import FOOTER_EXACT, HEAD_MEANING, HEAD_RANKED, NOTHING_LINE
from src.domain.email_search_time import TimeWindow
from tests.unit.domain.email_search_fixtures import booking, invoice, policy, receipts

SEPTEMBER = TimeWindow("period", date(2026, 9, 1), date(2026, 9, 30), True, "September 2026")


def _cand(card, group=GROUP_ABOUT, keys=(), z=None):
    return Candidate(card=card, group=group, lists=1, z=z, hit_keys=tuple(keys))


def _ranked(cands, *, absent=(), without=None, total=None, uncarded=0):
    result = RankedResult(tuple(cands), tuple(absent), without, not cands and not absent)
    lines = fold(cands)
    return render_ranked(result, [ln for ln in lines if ln.group != GROUP_MEANING],
                         [ln for ln in lines if ln.group == GROUP_MEANING],
                         total=len([c for c in cands if c.group != GROUP_MEANING]) if total is None else total,
                         scope_all=False, uncarded=uncarded, has_topic=True)


def _joined(result):
    return "\n".join(s.text for s in result.sections)


def _kinds(result):
    return [s.kind for s in result.sections]


def test_a_ranked_result_splits_into_tool_mail_tool_mail_tool():
    rs = receipts()
    cands = [_cand(invoice(), keys=("v:fa0137",)), *(_cand(c, GROUP_TRANSLATED) for c in rs),
             _cand(booking(), GROUP_MEANING, z=3.1)]
    out = _ranked(cands, absent=("example air",), without=2, total=14, uncarded=3)
    assert _joined(out) == out.text
    assert _kinds(out) == [SECTION_TOOL, SECTION_MAIL, SECTION_TOOL, SECTION_MAIL, SECTION_TOOL]
    head, matched, meaning_head, meaning, tail = (s.text for s in out.sections)
    assert head == HEAD_RANKED and meaning_head == HEAD_MEANING
    assert matched == "\n".join([render_result_line(invoice(), ("v:fa0137",)), render_result_line(rs[-1], ()),
                                 "  + 11 more of the same kind from supermercado.example, Jan 2025 – Sep 2026"])
    assert meaning == render_result_line(booking(), ())
    assert tail.split("\n") == ['No indexed mail names "example air".', "Without it: 2 mails match the rest.",
                                FOOTER_EXACT, "Showing 13 of 14 matching mails.",
                                "Re-carding in progress: 3 indexed mails still have old entries and are not searched "
                                "here."]


def test_mail_sections_hold_only_card_and_fold_lines():
    rs = receipts()
    out = _ranked([*(_cand(c) for c in rs), _cand(policy(), GROUP_MEANING, z=2.0)], uncarded=1)
    for section in out.sections:
        if section.kind == SECTION_MAIL:
            assert all(line.startswith(("• [", "  → ", "  matched: ", "  + ")) for line in section.text.split("\n"))
        else:
            assert "• [" not in section.text


def test_shown_are_the_cards_on_a_line_of_their_own_in_order():
    rs = receipts()
    cands = [_cand(invoice()), *(_cand(c, GROUP_TRANSLATED) for c in rs), _cand(booking(), GROUP_MEANING, z=3.1)]
    out = _ranked(cands)
    assert tuple(c.mail.email_id for c in out.shown) == out.shown_ids == (
        "msg_invoice01", "msg_receipt_2026-09-22", "msg_rome01")


def test_only_meaning_lines_keep_both_heads_as_one_tool_section():
    out = _ranked([_cand(booking(), GROUP_MEANING, z=3.1)])
    assert _joined(out) == out.text
    assert _kinds(out) == [SECTION_TOOL, SECTION_MAIL]
    assert out.sections[0].text == f"{HEAD_RANKED}\n{HEAD_MEANING}"


def test_a_list_result_is_tool_head_mail_lines_tool_recard():
    rs = [c for c in receipts() if c.mail.sent_day.month == 9 and c.mail.sent_day.year == 2026]
    lines = fold([_cand(c) for c in sorted(rs, key=lambda c: c.mail.sent_at, reverse=True)])
    out = render_list(SEPTEMBER, lines, count=len(rs), uncarded=2)
    assert _joined(out) == out.text
    assert _kinds(out) == [SECTION_TOOL, SECTION_MAIL, SECTION_TOOL]
    assert out.sections[0].text.startswith("2 indexed mails for September 2026")
    assert out.sections[2].text.startswith("Re-carding in progress: 2 ")
    assert tuple(c.mail.email_id for c in out.shown) == out.shown_ids


def test_an_empty_list_and_a_nothing_are_one_tool_section():
    empty = render_list(SEPTEMBER, [], count=0, uncarded=0)
    nothing = _ranked([], absent=("example air",))
    for out in (empty, nothing):
        assert len(out.sections) == 1 and out.sections[0].kind == SECTION_TOOL
        assert out.sections[0].text == out.text and out.shown == ()
    assert nothing.text.startswith(NOTHING_LINE)
