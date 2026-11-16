"""_format_email_search_compact - the null-since intro makes no claim about the index; the empty-index line (the v2
index block's nothing line, email index v2 §5.4) covers notes."""
import json

from src.domain.email_search_texts import NOTHING_LINE
from src.infrastructure.delegation_engine import _format_email_search_compact
from tests.unit.domain.email_search_fixtures import index_block


def _fmt(**parts) -> str:
    body = {"fresh": None, "by_key": [], "notes": [], **parts}
    counter = iter(range(10 ** 6))
    return _format_email_search_compact(json.dumps(body), nonce=lambda: f"{next(counter):08d}")


def _fresh(**over) -> dict:
    return {"since": None, "until": None, "tz": "UTC", "truncated": False, "listed": 1,
            "emails": [{"email_id": "m1", "from": "shop@example.com", "subject": "Invoice FA-0137",
                        "date": "2026-03-04 09:00", "snippet": "first line"}], **over}


def test_null_since_intro_does_not_claim_the_index_was_never_built():
    out = _fmt(fresh=_fresh())
    assert ("Recent mail from the live mailbox, newest first, headers only (sender, subject, first line); "
            "promotions, spam and trash are not listed:\n") in out
    assert "never been built" not in out


def test_all_deduped_lines_start_with_the_live_mailbox_phrase():
    with_since = _fmt(index=index_block("e1"), fresh=_fresh(since="2026-03-03 00:00", emails=[], listed=2))
    without = _fmt(index=index_block("e1"), fresh=_fresh(emails=[], listed=2))
    assert "Recent mail from the live mailbox since 2026-03-03 00:00: all of it is already listed above." in with_since
    assert "\nRecent mail from the live mailbox: all of it is already listed above." in without


def test_nothing_in_the_index_line_prints_for_notes_alone():
    out = _fmt(index=index_block(), notes=["unavailable"])
    assert out.splitlines()[0] == NOTHING_LINE


def test_nothing_in_the_index_line_is_absent_when_the_index_has_hits_and_notes():
    out = _fmt(index=index_block("e1"), notes=["unavailable"])
    assert NOTHING_LINE not in out
