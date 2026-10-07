"""
Slack table delivery — prod log audit C-03 (docs/reviews/PROD_LOG_AUDIT_FOLLOWUP.md).

Slack rejects a table block with `invalid_blocks` when rows differ in width
(`uneven_table_rows_not_allowed`) or a raw_text cell is empty (`must be more than 0
characters`). The reply was already generated and paid for, yet the user got nothing.
"""
import pytest
from unittest.mock import AsyncMock

from slack_sdk.errors import SlackApiError

from src.adapters.slack.response_channel import SlackResponseChannel
from src.domain.language import LanguageCode
from src.domain.messaging import RichContent


def _channel() -> SlackResponseChannel:
    ch = SlackResponseChannel(AsyncMock(), "C1", "token", language=LanguageCode.EN)
    ch.send_long_text = AsyncMock(return_value={"ts": "t1"})
    return ch


def _table_rows(blocks):
    (table,) = [b for b in blocks if b["type"] == "table"]
    return table["rows"], table


def _texts(rows):
    return [[cell["text"] for cell in row] for row in rows]


class TestTableBlockShape:

    def test_short_row_is_padded_to_the_header_width(self):
        blocks = _channel()._build_generic_table_blocks(
            {"headers": ["a", "b", "c"], "rows": [{"cells": ["1", "2"]}]}
        )
        rows, _ = _table_rows(blocks)
        assert [len(r) for r in rows] == [3, 3]

    def test_long_row_widens_the_table_without_dropping_data(self):
        blocks = _channel()._build_generic_table_blocks(
            {"headers": ["a", "b"], "rows": [{"cells": ["1", "2", "3", "4"]}]}
        )
        rows, table = _table_rows(blocks)
        assert [len(r) for r in rows] == [4, 4]
        assert _texts(rows)[1] == ["1", "2", "3", "4"]
        assert len(table["column_settings"]) == 4

    def test_ragged_rows_without_headers_share_one_width(self):
        blocks = _channel()._build_generic_table_blocks(
            {"rows": [["1"], ["1", "2", "3"], ["1", "2"]]}
        )
        rows, table = _table_rows(blocks)
        assert {len(r) for r in rows} == {3}
        assert len(table["column_settings"]) == 3

    @pytest.mark.parametrize("empty", ["", None, "   "])
    def test_empty_cells_never_reach_slack_as_empty_text(self, empty):
        blocks = _channel()._build_generic_table_blocks(
            {"headers": ["a", "b"], "rows": [{"cells": ["x", empty]}]}
        )
        rows, _ = _table_rows(blocks)
        assert all(cell["text"].strip() for row in rows for cell in row)
        assert all(cell["text"] != "None" for row in rows for cell in row)
        assert _texts(rows)[1][0] == "x"

    def test_empty_header_is_also_filled(self):
        blocks = _channel()._build_generic_table_blocks(
            {"headers": ["a", ""], "rows": [{"cells": ["1", "2"]}]}
        )
        rows, _ = _table_rows(blocks)
        assert all(cell["text"].strip() for cell in rows[0])

    def test_flat_array_is_still_rechunked_by_header_count(self):
        blocks = _channel()._build_generic_table_blocks(
            {"headers": ["a", "b"], "rows": ["1", "2", "3", "4"]}
        )
        rows, _ = _table_rows(blocks)
        assert _texts(rows) == [["a", "b"], ["1", "2"], ["3", "4"]]

    def test_well_formed_table_is_unchanged(self):
        blocks = _channel()._build_generic_table_blocks(
            {"title": "T", "headers": ["a", "b"], "rows": [{"cells": ["1", "2"]}], "footer": "f"}
        )
        assert [b["type"] for b in blocks] == ["section", "table", "context"]
        rows, _ = _table_rows(blocks)
        assert _texts(rows) == [["a", "b"], ["1", "2"]]


class TestInvalidBlocksFallback:

    _RICH = RichContent(
        content_type="table",
        data={"title": "Prices", "headers": ["Item", "Cost"], "rows": [{"cells": ["Tea", "3"]}]},
        fallback_text="",
    )

    @pytest.mark.asyncio
    async def test_successful_post_sends_blocks_only(self):
        ch = _channel()
        ch.client.chat_postMessage = AsyncMock(return_value={"ts": "ok"})

        result = await ch.send_rich_content(self._RICH)

        assert result == {"ts": "ok"}
        ch.client.chat_postMessage.assert_awaited_once()
        ch.send_long_text.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["invalid_blocks", "invalid_blocks_format"])
    async def test_rejected_blocks_are_redelivered_as_plain_text(self, code):
        ch = _channel()
        ch.client.chat_postMessage = AsyncMock(
            side_effect=SlackApiError(code, {"error": code})
        )

        await ch.send_rich_content(self._RICH, thread_id="th1")

        ch.send_long_text.assert_awaited_once()
        text = ch.send_long_text.await_args.args[0]
        for expected in ("Prices", "Item", "Cost", "Tea", "3"):
            assert expected in text
        assert ch.send_long_text.await_args.kwargs["thread_id"] == "th1"

    @pytest.mark.asyncio
    async def test_other_slack_errors_still_raise(self):
        ch = _channel()
        ch.client.chat_postMessage = AsyncMock(
            side_effect=SlackApiError("channel_not_found", {"error": "channel_not_found"})
        )

        with pytest.raises(SlackApiError):
            await ch.send_rich_content(self._RICH)

        ch.send_long_text.assert_not_awaited()
