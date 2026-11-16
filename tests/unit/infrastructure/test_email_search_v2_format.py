"""_format_email_search_compact on a v2 result (email index v2 §5.4; slice 8, D4): the `index` block's sections in print
order, the tool's own text outside every wrapper (the head that says to open a mail before quoting it is an
instruction), each mail section in a wrapper of its own nonce, the reminder once, the hints, and the fresh and per-key
lists after it; a v1 result renders as before. The sections come from the real renderer. Invented cast only."""
import json
import re

from src.domain.email_search_fold import fold
from src.domain.email_search_merge import GROUP_ABOUT, GROUP_MEANING, GROUP_TRANSLATED, Candidate, RankedResult
from src.domain.email_search_render import render_ranked
from src.domain.email_search_texts import HEAD_MEANING, HEAD_RANKED, NOTHING_LINE
from src.domain.untrusted_text import EXTERNAL_REMINDER
from src.infrastructure.delegation_engine import _format_email_search_compact
from tests.unit.domain.email_search_fixtures import booking, invoice, receipts

BODY_HINT = "To read email body: delegate get_email_details"
ATT_HINT = "To read attachment: delegate open_email_attachment"
HEADER_HINT = "A header line has no body and no attachment names"


def _cand(card, group=GROUP_ABOUT, z=None):
    return Candidate(card=card, group=group, lists=1, z=z, hit_keys=())


def _index(cands, absent=(), uncarded=0):
    lines = fold(cands)
    result = render_ranked(RankedResult(tuple(cands), tuple(absent), None, not cands),
                           [ln for ln in lines if ln.group != GROUP_MEANING],
                           [ln for ln in lines if ln.group == GROUP_MEANING],
                           total=len([c for c in cands if c.group != GROUP_MEANING]), scope_all=False,
                           uncarded=uncarded, has_topic=True)
    return {"mode": result.mode, "sections": [[s.kind, s.text] for s in result.sections],
            "shown_ids": list(result.shown_ids), "folded_ids": {k: list(v) for k, v in result.folded_ids.items()},
            "has_attachments": any(c.attachments for c in result.shown)}, result


def _fmt(index, **parts) -> str:
    body = {"index": index, "fresh": None, "by_key": [], "notes": [], **parts}
    counter = iter(range(10 ** 6))
    return _format_email_search_compact(json.dumps(body), nonce=lambda: f"{next(counter):08d}")


def _blocks(out: str):
    return re.findall(r'<external_content nonce="(\d{8})"[^>]*>\n(.*?)\n</external_content nonce="\1">', out, re.S)


def _outside(out: str) -> str:
    return re.sub(r'<external_content nonce="(\d{8})"[^>]*>\n.*?\n</external_content nonce="\1">', "", out, flags=re.S)


def _full():
    rs = receipts()
    return _index([_cand(invoice()), *(_cand(c, GROUP_TRANSLATED) for c in rs), _cand(booking(), GROUP_MEANING, 3.0)],
                  absent=("example air",), uncarded=2)


def test_the_tool_text_is_outside_every_wrapper():
    index, result = _full()
    out = _fmt(index)
    blocks = _blocks(out)
    outside = _outside(out)
    for kind, text in index["sections"]:
        if kind == "tool":
            assert text in outside and not any(text in b for _, b in blocks)
    assert HEAD_RANKED in outside and HEAD_MEANING in outside
    assert 'No indexed mail names "example air".' in outside and "Re-carding in progress: 2" in outside


def test_each_mail_section_is_its_own_wrapper_with_its_own_nonce():
    index, result = _full()
    blocks = _blocks(_fmt(index))
    mail = [text for kind, text in index["sections"] if kind == "mail"]
    assert [b for _, b in blocks] == mail and len(mail) == 2
    assert len({n for n, _ in blocks}) == 2
    assert "• [msg_invoice01]" in blocks[0][1] and "+ 11 more of the same kind" in blocks[0][1]
    assert "• [msg_rome01]" in blocks[1][1]


def test_the_sections_print_in_order_after_one_reminder_and_no_v1_lines():
    index, _ = _full()
    out = _fmt(index)
    assert out.count(EXTERNAL_REMINDER) == 1 and out.startswith(EXTERNAL_REMINDER)
    assert out.index(HEAD_RANKED) < out.index("• [msg_invoice01]") < out.index(HEAD_MEANING) < out.index(
        "• [msg_rome01]") < out.index("Re-carding in progress")
    assert "Found " not in out and "Nothing in the index matched." not in out


def test_the_hints_follow_the_shown_cards_and_their_attachments():
    index, _ = _full()
    out = _fmt(index)
    assert BODY_HINT in out and ATT_HINT in out and HEADER_HINT not in out
    plain, _ = _index([_cand(invoice())])
    out = _fmt(plain)
    assert BODY_HINT in out and ATT_HINT not in out


def test_the_fresh_and_per_key_lists_still_follow():
    index, _ = _full()
    header = {"email_id": "msg_fresh01", "from": "info@school.example", "subject": "New term", "date": "2026-11-16 09:00",
              "snippet": "first line"}
    fresh = {"since": "2026-11-15 00:00", "until": None, "tz": "Europe/Lisbon", "truncated": False, "listed": 1,
             "emails": [header]}
    by_key = [{"key": "FA-0137", "truncated": False, "listed": 1, "emails": []}]
    out = _fmt(index, fresh=fresh, by_key=by_key)
    assert out.index("Re-carding in progress") < out.index("Recent mail from the live mailbox") < out.index(
        'Matches for "FA-0137" (any date): all already listed above.')
    assert len(_blocks(out)) == 3 and HEADER_HINT in out


def test_a_nothing_result_is_tool_text_only():
    index, _ = _index([], absent=("example air",))
    out = _fmt(index)
    assert out.startswith(NOTHING_LINE) and _blocks(out) == [] and EXTERNAL_REMINDER not in out
    assert BODY_HINT not in out


def test_a_v1_result_renders_as_before():
    v1 = {"count": 1, "emails": [{"email_id": "e1", "from": "info@school.example", "date": "2026-03-02",
                                  "text": "Example School parents' meeting", "attachments": []}],
          "fresh": None, "by_key": [], "notes": []}
    counter = iter(range(10 ** 6))
    out = _format_email_search_compact(json.dumps(v1), nonce=lambda: f"{next(counter):08d}")
    assert "Found 1 email(s) in the index:" in out and len(_blocks(out)) == 1
