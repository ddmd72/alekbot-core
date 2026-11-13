"""EmailSearchAgent / result formatter paths: a failing watermark read, history-context guards, header lines without text."""
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from src.agents.email_search_agent import EmailSearchAgent
from src.infrastructure.delegation_engine import _format_email_search_compact
from tests.unit.agents.test_email_search_agent_fresh_headers import FakeService, _agent, _hit, _meta, _search


class _BrokenWatermark(FakeService):
    async def index_watermark(self, user_id):
        raise RuntimeError("indexing state unreadable")


async def test_a_failing_watermark_read_lists_fresh_mail_with_no_lower_bound():
    service = _BrokenWatermark(index=[_hit("e1")])
    service.listing[None] = ([_meta("m1")], False)

    _, body = await _search(_agent(service, {}))

    assert service.fresh_calls()[0]["after"] is None
    assert body["count"] == 1 and body["fresh"]["since"] is None
    assert [e["email_id"] for e in body["fresh"]["emails"]] == ["m1"]


@pytest.mark.parametrize("result", [None, 42, ["not", "a", "str"]])
def test_history_context_of_a_non_string_result_is_none(result: Any):
    assert EmailSearchAgent._build_email_history_context(result, "q") is None


def test_history_context_of_invalid_json_is_none():
    assert EmailSearchAgent._build_email_history_context("No emails found matching your query.", "q") is None


def test_history_context_of_json_that_is_not_an_object_is_none():
    assert EmailSearchAgent._build_email_history_context(json.dumps(["m1"]), "q") is None


def test_history_context_without_emails_or_headers_is_none():
    result = json.dumps({"count": 0, "emails": [], "fresh": {"emails": []}, "by_key": [{"key": "FA-0137", "emails": []}],
                         "notes": []})
    assert EmailSearchAgent._build_email_history_context(result, "q") is None


def _fmt(**parts) -> str:
    body = {"count": 0, "emails": [], "fresh": None, "by_key": [], "notes": [], **parts}
    counter = iter(range(10 ** 6))
    return _format_email_search_compact(json.dumps(body), nonce=lambda: f"{next(counter):08d}")


def test_a_header_without_a_snippet_prints_no_arrow_line():
    out = _fmt(by_key=[{"key": "FA-0137", "truncated": False, "listed": 1, "emails": [
        {"email_id": "m1", "from": "shop@example.com", "subject": "Invoice FA-0137", "date": "2026-03-04 09:00",
         "snippet": ""}]}])
    assert "• [m1] shop@example.com | 2026-03-04 09:00 | Invoice FA-0137" in out
    assert "→" not in out


def test_an_index_hit_without_text_prints_no_arrow_line():
    out = _fmt(count=1, emails=[{"email_id": "e1", "from": "clara@example.com", "date": "2026-02-10",
                                 "text": "", "attachments": []}])
    assert "• [e1] clara@example.com | 2026-02-10" in out and "→" not in out


def test_a_non_string_result_is_stringified():
    assert _format_email_search_compact(["m1"]) == "['m1']"  # type: ignore[arg-type]


def test_json_that_is_not_an_object_is_returned_as_is():
    assert _format_email_search_compact('["m1"]') == '["m1"]'


def test_an_object_with_nothing_to_show_is_returned_as_is():
    raw = json.dumps({"count": 0, "emails": [], "fresh": None, "by_key": [], "notes": []})
    assert _format_email_search_compact(raw) == raw
