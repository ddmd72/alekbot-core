"""Email search fold (email index v2 §5.4; search slice 4): template mail folded by code fields only, an exact id or
amount hit never folded, groups never mixed. Invented cast only."""
from datetime import date

from src.domain.email_search_merge import GROUP_ABOUT, GROUP_MEANING, Candidate
from src.domain.email_search_fold import fold
from tests.unit.domain.email_search_fixtures import (
    booking, build, invoice, receipt, receipts, school_news, school_notice,
)


def _cand(card, group=GROUP_ABOUT, keys=()):
    return Candidate(card=card, group=group, lists=1, z=None, hit_keys=tuple(keys))


def _id(card):
    return card.mail.email_id


def test_twelve_receipts_of_one_template_are_one_line_with_the_newest_and_eleven_folded():
    cards = receipts()
    lines = fold([_cand(c) for c in cards])
    assert len(lines) == 1
    line = lines[0]
    assert _id(line.card) == "msg_receipt_2026-09-22" and len(line.folded) == 11
    assert [_id(c) for c in line.folded][0] == "msg_receipt_2026-09-08"       # newest of the rest first


def test_the_receipt_hit_by_its_printed_number_stays_its_own_line():
    cards = receipts()
    printed = next(c for c in cards if _id(c) == "msg_receipt_2025-05-20")
    lines = fold([_cand(c, keys=("v:0931",) if c is printed else ()) for c in cards])
    assert len(lines) == 2
    own = next(line for line in lines if line.card is printed)
    assert own.folded == () and own.matched == ("v:0931",)
    rest = next(line for line in lines if line.card is not printed)
    assert len(rest.folded) == 10 and _id(rest.card) == "msg_receipt_2026-09-22"


def test_a_sender_or_word_hit_never_stops_folding():
    cards = receipts()
    lines = fold([_cand(c, keys=("e:supermercado_exemplo_lda", "lw:receipt")) for c in cards])
    assert len(lines) == 1 and len(lines[0].folded) == 11


def test_the_line_keeps_the_hit_keys_of_the_member_it_shows():
    cards = receipts()
    newest = next(c for c in cards if _id(c) == "msg_receipt_2026-09-22")
    lines = fold([_cand(c, keys=("lw:receipt",) if c is newest else ("e:other",)) for c in cards])
    assert lines[0].matched == ("lw:receipt",)


def test_the_group_stands_where_its_best_ranked_member_stands():
    first, second = receipt(date(2026, 9, 8)), receipt(date(2025, 1, 14))
    lines = fold([_cand(first), _cand(invoice()), _cand(second)])
    assert [_id(line.card) for line in lines] == [_id(first), "msg_invoice01"]
    assert [_id(c) for c in lines[0].folded] == [_id(second)]


def test_different_sender_domains_never_fold():
    a = school_news("msg_a", "Example School news, February", date(2026, 2, 10))
    b = build("Example School news, February", "Example School\nNews\n", date(2026, 2, 11), "msg_b",
              "Example School", "other-school.example", doc_type="letter", dates=[], key_values=[])
    assert a.template_key != b.template_key
    assert len(fold([_cand(a), _cand(b)])) == 2


def test_the_same_template_with_a_different_month_in_the_subject_folds():
    feb = school_news("msg_feb", "Example School news, February", date(2026, 2, 10))
    apr = school_news("msg_apr", "Example School news, April", date(2026, 4, 2))
    lines = fold([_cand(feb), _cand(apr)])
    assert len(lines) == 1 and _id(lines[0].card) == "msg_apr" and [_id(c) for c in lines[0].folded] == ["msg_feb"]


def test_a_meaning_only_card_never_folds_into_a_matched_group():
    matched, meaning_only = receipt(date(2026, 9, 8)), receipt(date(2025, 1, 14))
    lines = fold([_cand(matched, GROUP_ABOUT), _cand(meaning_only, GROUP_MEANING)])
    assert len(lines) == 2 and all(line.folded == () for line in lines)
    assert [line.group for line in lines] == [GROUP_ABOUT, GROUP_MEANING]


def test_a_card_alone_is_a_plain_line_and_the_order_is_kept():
    cards = [booking(), invoice(), school_notice()]
    lines = fold([_cand(c) for c in cards])
    assert [line.card for line in lines] == cards and all(line.folded == () for line in lines)
    assert fold([]) == ()
