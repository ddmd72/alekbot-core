"""Email search render (email index v2 §5.4, §2.5; search slice 4): the ranked, list and nothing blocks with their
exact code texts. Invented cast only."""
from datetime import date, timedelta

from src.domain.email_card_text import render_result_line
from src.domain.email_search_fold import fold
from src.domain.email_search_merge import (
    GROUP_ABOUT, GROUP_LABELLED, GROUP_MEANING, GROUP_SLOT, GROUP_TRANSLATED, Candidate, RankedResult,
)
from src.domain.email_search_render import render_list, render_ranked
from src.domain.email_search_texts import HEAD_MEANING, HEAD_RANKED
from src.domain.email_search_time import TimeWindow
from tests.unit.domain.email_search_fixtures import booking, invoice, receipts, school_news

SEPTEMBER = TimeWindow("period", date(2026, 9, 1), date(2026, 9, 30), True, "September 2026")


def _cand(card, group=GROUP_ABOUT, keys=(), z=None):
    return Candidate(card=card, group=group, lists=1, z=z, hit_keys=tuple(keys))


def _ranked(cands, absent=(), without=None, nothing=False):
    return RankedResult(tuple(cands), tuple(absent), without, nothing)


def _render(cands, *, total=None, scope_all=False, uncarded=0, has_topic=True, absent=(), without=None):
    result = _ranked(cands, absent, without, nothing=not cands and not absent)
    lines = fold(cands)
    return render_ranked(result, [ln for ln in lines if ln.group != GROUP_MEANING],
                         [ln for ln in lines if ln.group == GROUP_MEANING],
                         total=len([c for c in cands if c.group != GROUP_MEANING]) if total is None else total,
                         scope_all=scope_all, uncarded=uncarded, has_topic=has_topic)


def _school(n, start=date(2026, 2, 1)):
    """n Example School mails of n different templates (the subject's letters differ), one per day from `start`."""
    return [school_news(f"msg_{start.month:02d}_{i:02d}", f"Example School: topic words{'x' * i}",
                        start + timedelta(days=i)) for i in range(n)]


def test_a_ranked_block_has_the_head_the_lines_the_fold_the_meaning_section_the_footer_and_the_count():
    rs = receipts()
    cands = [_cand(invoice(), keys=("v:fa0137",)), *(_cand(c, GROUP_TRANSLATED) for c in rs),
             _cand(booking(), GROUP_MEANING, z=3.1)]
    out = _render(cands, total=14, uncarded=0)
    inv, newest, rome = (render_result_line(c.card, c.hit_keys) for c in (cands[0], cands[-2], cands[-1]))
    expected = "\n".join([HEAD_RANKED, inv, render_result_line(rs[-1], ()),
                          "  + 11 more of the same kind from supermercado.example, Jan 2025 – Sep 2026",
                          HEAD_MEANING, rome, "No other indexed mail matches all of these.",
                          "Showing 13 of 14 matching mails."])
    assert newest.startswith("• [msg_receipt_2026-09-22]")
    assert out.text == expected and out.mode == "ranked" and out.total == 14
    assert out.shown_ids == ("msg_invoice01", "msg_receipt_2026-09-22", "msg_rome01")
    assert out.folded_ids["msg_receipt_2026-09-22"][0] == "msg_receipt_2026-09-08"
    assert len(out.ranked_ids) == 14


def test_the_footer_needs_a_topic_word_and_a_line_in_group_about():
    only_slot = _render([_cand(invoice(), GROUP_SLOT)])
    assert "No other indexed mail" not in only_slot.text
    no_topic = _render([_cand(invoice())], has_topic=False)
    assert "No other indexed mail" not in no_topic.text
    assert "No other indexed mail matches all of these." in _render([_cand(invoice())]).text


def test_group_about_shows_ten_lines_and_groups_one_to_three_eight_together():
    about = [_cand(c, GROUP_ABOUT) for c in _school(14)]
    rest = [_cand(c, g) for c, g in zip(_school(12, date(2026, 3, 1)), [GROUP_TRANSLATED] * 4 + [GROUP_LABELLED] * 4 +
                                         [GROUP_SLOT] * 4)]
    out = _render([*about, *rest])
    assert len(out.shown_ids) == 18 and out.text.endswith("Showing 18 of 26 matching mails.")
    assert out.ranked_ids[:14] == tuple(c.card.mail.email_id for c in about)


def test_meaning_only_lines_are_capped_at_five():
    out = _render([_cand(c, GROUP_MEANING, z=3.0) for c in _school(8)], total=0)
    assert len(out.shown_ids) == 5 and HEAD_MEANING in out.text and "Showing" not in out.text


def test_scope_all_raises_groups_zero_to_three_to_fifty_lines():
    cands = [_cand(c) for c in _school(25)]
    assert len(_render(cands).shown_ids) == 10
    everything = _render(cands, scope_all=True)
    assert len(everything.shown_ids) == 25 and "Showing" not in everything.text


def test_nothing_with_absent_words_the_without_count_and_the_recard_line():
    out = _render([], absent=("air", "tickets"), without=4, uncarded=2)
    assert out.mode == "nothing" and out.shown_ids == () and out.total == 0
    assert out.text == "\n".join([
        "No indexed mail matches: for the index this answer is final.",
        'No indexed mail names "air".', 'No indexed mail names "tickets".',
        "Without these words: 4 mails match the rest.",
        "Re-carding in progress: 2 indexed mails still have old entries and are not searched here."])


def test_one_absent_word_and_its_without_line():
    out = _render([_cand(invoice(), GROUP_SLOT)], absent=("air",), without=3)
    assert out.mode == "ranked"
    assert 'No indexed mail names "air".\nWithout it: 3 mails match the rest.' in out.text


def test_a_plain_nothing_is_one_line():
    out = _render([])
    assert out.text == "No indexed mail matches: for the index this answer is final." and out.mode == "nothing"


def test_the_singular_forms():
    assert 'Without it: 1 mail matches the rest.' in _render([_cand(invoice(), GROUP_SLOT)], absent=("air",),
                                                           without=1).text
    assert "Without these words: 1 mail matches the rest." in _render(
        [_cand(invoice(), GROUP_SLOT)], absent=("air", "tickets"), without=1).text
    assert _render([_cand(invoice())], uncarded=1).text.endswith(
        "Re-carding in progress: 1 indexed mail still has old entries and is not searched here.")
    one = render_list(SEPTEMBER, fold([_cand(invoice())]), count=1, uncarded=0)
    assert one.text.splitlines()[0] == "1 indexed mail for September 2026, shown."


def test_a_list_block_all_shown_with_the_fold_and_the_recard_line():
    cards = [receipts()[-1], invoice(), receipts()[-2]]
    out = render_list(SEPTEMBER, fold([_cand(c) for c in cards]), count=3, uncarded=5)
    assert out.mode == "list" and out.total == 3
    assert out.text.splitlines() == [
        "3 indexed mails for September 2026, all shown, newest first.",
        *render_result_line(cards[0], ()).splitlines(),
        "  + 1 more of the same kind from supermercado.example, Sep 2026",
        *render_result_line(cards[1], ()).splitlines(),
        "Re-carding in progress: 5 indexed mails still have old entries and are not searched here."]
    assert out.shown_ids == ("msg_receipt_2026-09-22", "msg_invoice01")
    assert out.folded_ids == {"msg_receipt_2026-09-22": ("msg_receipt_2026-09-08",)}


def test_a_list_cut_at_fifty_lines_says_how_many_are_shown():
    cards = _school(60)
    out = render_list(SEPTEMBER, fold([_cand(c) for c in cards]), count=60, uncarded=0)
    assert out.text.splitlines()[0] == "60 indexed mails for September 2026; the newest 50 are shown."
    assert len(out.shown_ids) == 50 and len(out.ranked_ids) == 60


def test_an_empty_period_is_one_line():
    out = render_list(SEPTEMBER, (), count=0, uncarded=0)
    assert out.text == "No indexed mail for September 2026." and out.shown_ids == () and out.total == 0
    assert render_list(SEPTEMBER, (), count=0, uncarded=1).text.splitlines()[-1].startswith("Re-carding")
