"""Email search merge (email index v2 §5.3, the U rule; search slice 4): rank groups, list counts, the time filter,
an absent word and the honest nothing. Invented cast only."""
from datetime import date
from typing import Dict, Mapping, Optional

from src.domain.drive_match import MatchTier, SourceClass
from src.domain.drive_tiers import Cover, FileMatch
from src.domain.email_search_merge import (
    GROUP_ABOUT, GROUP_LABELLED, GROUP_MEANING, GROUP_SLOT, GROUP_TRANSLATED, merge,
)
from src.domain.email_search_time import TimeWindow
from tests.unit.domain.email_search_fixtures import booking, invoice, policy, receipt, school_notice

TODAY = date(2026, 11, 13)
FLOOR = 2.5
INVOICE, BOOKING, POLICY, NOTICE = invoice(), booking(), policy(), school_notice()
RECEIPT = receipt(date(2026, 6, 4))
CARDS: Mapping[str, object] = {c.mail.email_id: c for c in (INVOICE, BOOKING, POLICY, NOTICE, RECEIPT)}


def _id(card):
    return card.mail.email_id


def _match(card, tier, *keys):
    return FileMatch(_id(card), tier, (Cover("w", SourceClass.VERIFIED_RECORD, "record", tuple(keys)),))


def _merge(*, matches=None, exact=None, people=None, labels=None, window_members=(), meaning=None, window=None,
           absent=(), without=None):
    return merge(matches=matches or {}, exact=exact or {}, people=people or {}, labels=labels or {},
                 window_members=set(window_members), meaning=meaning, floor=FLOOR, cards=CARDS, window=window,
                 today=TODAY, absent=absent, without=without)


def _order(result):
    return [_id(c.card) for c in result.ranked]


def _groups(result) -> Dict[str, int]:
    return {_id(c.card): c.group for c in result.ranked}


def test_the_rank_groups_run_about_translated_labelled_slot_meaning():
    result = _merge(
        matches={_id(RECEIPT): _match(RECEIPT, MatchTier.LABELLED), _id(BOOKING): _match(BOOKING, MatchTier.TRANSLATED),
                 _id(INVOICE): _match(INVOICE, MatchTier.ABOUT, "v:fa0137")},
        people={_id(NOTICE): ("e:jamie",)}, meaning={_id(POLICY): 3.0})
    assert _order(result) == [_id(INVOICE), _id(BOOKING), _id(RECEIPT), _id(NOTICE), _id(POLICY)]
    assert _groups(result) == {_id(INVOICE): GROUP_ABOUT, _id(BOOKING): GROUP_TRANSLATED,
                               _id(RECEIPT): GROUP_LABELLED, _id(NOTICE): GROUP_SLOT, _id(POLICY): GROUP_MEANING}


def test_an_exact_key_hit_is_group_about_and_a_mentioned_tier_too():
    result = _merge(exact={_id(INVOICE): ("v:fa0137",)}, matches={_id(POLICY): _match(POLICY, MatchTier.MENTIONED)})
    assert _groups(result) == {_id(INVOICE): GROUP_ABOUT, _id(POLICY): GROUP_ABOUT}


def test_the_best_group_wins_and_every_list_counts():
    result = _merge(matches={_id(INVOICE): _match(INVOICE, MatchTier.LABELLED, "lw:invoice")},
                    exact={_id(INVOICE): ("v:fa0137",)}, labels={_id(INVOICE): ("l:type:invoice",)},
                    window_members={_id(INVOICE)}, meaning={_id(INVOICE): 4.0})
    only = result.ranked[0]
    assert only.group == GROUP_ABOUT and only.lists == 5 and only.z == 4.0
    assert only.hit_keys == ("lw:invoice", "v:fa0137", "l:type:invoice")


def test_more_lists_outrank_a_better_meaning_score_inside_a_group():
    result = _merge(matches={_id(BOOKING): _match(BOOKING, MatchTier.ABOUT, "e:rome"),
                             _id(INVOICE): _match(INVOICE, MatchTier.ABOUT, "v:fa0137")},
                    labels={_id(INVOICE): ("l:domain:home",)}, people={_id(INVOICE): ("e:jamie",)},
                    meaning={_id(BOOKING): 9.0})
    assert _order(result) == [_id(INVOICE), _id(BOOKING)]
    assert [c.lists for c in result.ranked] == [3, 2]


def test_ties_go_by_meaning_score_then_the_newest_display_time():
    by_z = _merge(meaning={_id(POLICY): 3.0, _id(RECEIPT): 5.0})
    assert _order(by_z) == [_id(RECEIPT), _id(POLICY)]
    # the notice's display time is its event (3 Mar 2026), the invoice's its sent day (15 Sep 2026)
    by_when = _merge(people={_id(NOTICE): ("e:jamie",), _id(INVOICE): ("e:jamie",)})
    assert _order(by_when) == [_id(INVOICE), _id(NOTICE)]


def test_a_meaning_hit_under_the_floor_is_not_a_candidate_but_keeps_its_score_on_another_list():
    result = _merge(meaning={_id(POLICY): 1.0, _id(INVOICE): 1.0}, labels={_id(INVOICE): ("l:type:invoice",)})
    assert _order(result) == [_id(INVOICE)]
    assert result.ranked[0].z == 1.0 and result.ranked[0].group == GROUP_SLOT and result.ranked[0].lists == 1


def test_a_card_missing_from_the_card_map_is_skipped():
    result = _merge(people={"msg_unknown": ("e:jamie",), _id(NOTICE): ("e:jamie",)})
    assert _order(result) == [_id(NOTICE)]


def test_the_time_filter_applies_to_every_group_when_the_text_names_a_time():
    window = TimeWindow("period", date(2026, 3, 1), date(2026, 3, 31), True, "March 2026")
    result = _merge(matches={_id(NOTICE): _match(NOTICE, MatchTier.ABOUT), _id(INVOICE): _match(INVOICE, MatchTier.ABOUT)},
                    meaning={_id(RECEIPT): 5.0, _id(POLICY): 4.0}, people={_id(BOOKING): ("e:rome",)},
                    window_members={_id(NOTICE), _id(POLICY)}, window=window)
    assert _order(result) == [_id(NOTICE), _id(POLICY)]     # the policy was sent 20 Mar 2026; the rest is out


def test_a_window_the_text_does_not_name_adds_candidates_and_never_removes_one():
    window = TimeWindow("upcoming", TODAY, date(2027, 11, 13), False, "")
    result = _merge(matches={_id(INVOICE): _match(INVOICE, MatchTier.ABOUT)}, window_members={_id(POLICY)},
                    window=window)
    assert _order(result) == [_id(INVOICE), _id(POLICY)]
    assert _groups(result)[_id(POLICY)] == GROUP_SLOT


def test_latest_orders_each_group_by_sent_time_newest_first():
    window = TimeWindow("latest", None, None, False, "")
    result = _merge(people={_id(BOOKING): ("e:rome",), _id(INVOICE): ("e:rome",), _id(NOTICE): ("e:rome",)},
                    meaning={_id(POLICY): 9.0}, window=window)
    assert _order(result) == [_id(INVOICE), _id(NOTICE), _id(BOOKING), _id(POLICY)]


def test_an_absent_word_drops_the_tier_matches_and_keeps_the_slot_lists_and_the_meaning_list():
    result = _merge(matches={_id(INVOICE): _match(INVOICE, MatchTier.ABOUT)}, people={_id(NOTICE): ("e:jamie",)},
                    meaning={_id(POLICY): 3.0}, absent=("air",), without=4)
    assert _order(result) == [_id(NOTICE), _id(POLICY)]
    assert result.absent == ("air",) and result.without == 4 and not result.nothing


def test_the_answer_is_nothing_only_when_every_list_is_empty_and_no_word_is_absent():
    assert _merge().nothing and _merge().ranked == ()
    assert not _merge(absent=("air",)).nothing
    assert not _merge(meaning={_id(POLICY): 3.0}).nothing
    assert _merge(meaning={_id(POLICY): 1.0}).nothing
    assert _merge(matches={_id(INVOICE): _match(INVOICE, MatchTier.ABOUT)}, absent=("air",)).ranked == ()


def test_no_meaning_list_is_the_same_as_an_empty_one():
    none: Optional[Mapping[str, float]] = None
    assert _merge(meaning=none).nothing and _merge(meaning={}).nothing
