"""The drive connector's `wide` and `batch` end to end on the drive ASGI app (DRIVE_MCP_CONNECTOR_RFC §14.2-§14.7, E17):
`tools/list` shows the new parameters (their texts from the tokens) and `maxResultSizeChars` on `search_drive` and
`find_passages`; the arguments reach the search service through the read facade; the result carries the next-batch
line quoting the question and scope, the end line, the search line in both forms; a bad batch is a correctable
request error; the span and the log line carry `wide`, `batch`, `shown` and `more`, never the question. The world is
`test_drive_mcp_tools`' (production `DriveOperations` and facade over fakes); texts are markers. Invented items only."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, List

import pytest

from src.composition import drive_mcp_setup
from src.domain import drive_connector_contract as c
from src.domain.drive_excerpt_caps import ExcerptCaps
from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchResult
from src.domain.drive_search_caps import SearchCaps
from src.domain.drive_wide_batch import BAD_BATCH
from src.infrastructure.drive_read_operations import DriveReadOperations
from tests.support.drive_connector_texts import marker
from tests.support.drive_service_connections import ref
from tests.unit.composition.test_drive_mcp_tools import PLAN, QUESTION, Search, World, text_of, what_to_do
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result
from tests.unit.infrastructure.test_drive_read_operations import CTX, Recorder


class PagedSearch(Search):
    """The search service as far as these tests need it: a find returns `caps.k` passages of Tom's plan (the caps the
    call reached the service with) and `more` while `batch` is under `last`; a past-the-end batch selects nothing new;
    a search shows `caps.about` cards out of `matched`."""

    def __init__(self) -> None:
        super().__init__()
        self.last = 3
        self.matched = 15

    async def excerpts(self, user_id: str, request: Any, **kw: Any):
        self.queries.append(request.question)
        caps, batch = kw.get("caps", ExcerptCaps.DEFAULT), kw.get("batch", 1)
        if batch > self.last:
            return result(question=request.question, ref_tag="a1",
                          repeated=tuple((PLAN.item_id, 1, i) for i in range(3)))
        start = (batch - 1) * caps.k
        file = excerpt_file(PLAN.item_id, PLAN.path, [chunk(PLAN.item_id, i, f"Step {i + 1} after the first review.")
                                                      for i in range(start, start + caps.k)], ref_tag="a1")
        return result(file, question=request.question, ref_tag="a1", more=batch < self.last)

    async def search(self, user_id: str, request: Any, **kw: Any) -> DriveSearchResult:
        from src.domain.drive_match import MatchTier
        from src.domain.drive_tier_result import CoverageFacts, GroupMember, TierBlock, TieredResult
        self.queries.append(request.query)
        caps = kw.get("caps", SearchCaps.DEFAULT)
        shown = min(caps.about, self.matched)
        cards = tuple(DriveSearchCard(ref=ref(f"p{i}"), item_id=f"p{i}", name=f"plan {i}.docx", path=f"R&D/plan {i}.docx",
                                      kind="docx", is_folder=False, summary="Tom's plan | Word document",
                                      nonce=f"nonce{i:04d}", tier=MatchTier.ABOUT.value, ref_tag="a1")
                      for i in range(shown))
        rest = tuple(GroupMember("R&D", "docx", "2026-03") for _ in range(self.matched - shown))
        tiered = TieredResult(concepts=("plan",), coverage=CoverageFacts(),
                              blocks=(TierBlock(MatchTier.ABOUT, self.matched, rest=rest, nonce="nonceg001"),))
        return DriveSearchResult(status=STATUS_OK, cards=cards, tiered=tiered, ref_tag="a1")


@pytest.fixture
async def world():
    w = World()
    w.search = PagedSearch()
    return await w.build()


def lines(text: str) -> List[str]:
    return text.split("\n")


# -- tools/list -------------------------------------------------------------------------------------------------------

async def test_the_new_parameters_are_listed_with_their_token_texts(world):
    tools = {t["name"]: t for t in await world.list_tools()}
    search = tools["search_drive"]["inputSchema"]["properties"]
    find = tools["find_passages"]["inputSchema"]["properties"]
    assert list(search)[-1] == "wide" and list(find)[-2:] == ["wide", "batch"]
    assert search["wide"]["description"] == marker("search_drive.wide")
    assert find["wide"]["description"] == marker("find_passages.wide")
    assert find["batch"]["description"] == marker("find_passages.batch")
    assert {"wide", "batch"}.isdisjoint(tools["find_passages"]["inputSchema"].get("required", []))


async def test_search_and_find_declare_the_result_size_as_open_does(world):
    tools = {t["name"]: t for t in await world.list_tools()}
    for name in ("search_drive", "find_passages", "open_drive_file"):
        assert tools[name]["_meta"] == {drive_mcp_setup.MAX_RESULT_SIZE_META_KEY: 100_000}, name


# -- find_passages ----------------------------------------------------------------------------------------------------

async def test_a_find_with_more_offers_the_next_batch_quoting_the_question_and_scope(world):
    text = text_of(await world.call("find_passages", question=QUESTION, folder="R&D"))
    arguments = f'question={json.dumps(QUESTION)}, folder="R&D", batch="2"'
    assert marker(c.BATCH_NEXT).format(batch=2, arguments=arguments) in lines(text)
    assert lines(text)[-2:] == [marker(c.BATCH_NEXT_WIDE), marker(c.LANGUAGE)]
    assert text.count("Step ") == ExcerptCaps.DEFAULT.k


async def test_a_wide_batch_reaches_the_service_and_keeps_wide_in_the_next_batch(world):
    text = text_of(await world.call("find_passages", question=QUESTION, file_refs=[ref("d1")], wide="YES",
                                    batch="2"))
    arguments = f'question={json.dumps(QUESTION)}, file_refs="{ref("d1")}", wide="yes", batch="3"'
    assert marker(c.BATCH_NEXT).format(batch=3, arguments=arguments) in lines(text)
    assert marker(c.BATCH_NEXT_WIDE) not in lines(text)
    assert f"Step {ExcerptCaps.WIDE.k + 1} after" in text and text.count("Step ") == ExcerptCaps.WIDE.k


async def test_a_batch_past_the_end_answers_the_end_line(world):
    text = text_of(await world.call("find_passages", question=QUESTION, batch=9))
    assert marker(c.BATCH_END) in lines(text)
    assert "Already shown earlier" not in text and not any(line.startswith("<BATCH_NEXT") for line in lines(text))


async def test_a_bad_batch_is_a_correctable_request_error_before_any_search(world):
    text = text_of(await world.call("find_passages", question=QUESTION, batch="next"))
    assert lines(text)[0] == marker(c.STATUS).format(status="refused")
    assert what_to_do(text) == marker(c.WHAT_TO_DO).format(line=BAD_BATCH.format(value="next"))
    assert world.search.queries == []


# -- search_drive -----------------------------------------------------------------------------------------------------

async def test_a_search_that_showed_fewer_than_it_matched_says_wide_or_a_folder(world):
    text = text_of(await world.call("search_drive", query="Tom's plan"))
    assert lines(text)[-1] == marker(c.SEARCH_MORE)


async def test_a_wide_search_keeps_only_the_folder_half_and_shows_more_cards(world):
    world.search.matched = SearchCaps.WIDE.about + 5
    text = text_of(await world.call("search_drive", query="Tom's plan", wide="true"))
    assert lines(text)[-1] == marker(c.SEARCH_MORE_WIDE) and text.count('ref="drive:a1:p') == SearchCaps.WIDE.about


async def test_a_wide_search_that_shows_every_match_has_no_search_line(world):
    text = text_of(await world.call("search_drive", query="Tom's plan", wide="true"))
    assert text.count('ref="drive:a1:p') == 15 and not any("SEARCH_MORE" in line for line in lines(text))


async def test_a_search_that_showed_everything_has_no_search_line(world):
    world.search.matched = 4
    text = text_of(await world.call("search_drive", query="Tom's plan"))
    assert not any("SEARCH_MORE" in line for line in lines(text))


# -- observability ----------------------------------------------------------------------------------------------------

class Span:
    def __init__(self) -> None:
        self.attributes: Dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


@pytest.fixture
def spans(monkeypatch):
    opened: List[Span] = []

    @contextmanager
    def start_span(name: str, attributes: Any = None):
        span = Span()
        span.attributes.update(attributes or {})
        opened.append(span)
        yield span

    monkeypatch.setattr(drive_mcp_setup, "start_span", start_span)
    return opened


async def test_the_find_span_and_log_line_carry_wide_batch_shown_and_more_never_the_question(world, spans, caplog):
    with caplog.at_level(logging.INFO):
        text_of(await world.call("find_passages", question=QUESTION, wide="yes", batch="2"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveMCP] tool=")]
    assert line.endswith(f" wide=1 batch=2 shown={ExcerptCaps.WIDE.k} more=1")
    (span,) = spans
    assert {k: span.attributes[k] for k in ("wide", "batch", "shown", "more")} == {
        "wide": 1, "batch": 2, "shown": ExcerptCaps.WIDE.k, "more": 1}
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert QUESTION not in everything and "first review" not in everything


async def test_the_search_log_line_counts_cards_and_says_more(world, spans, caplog):
    with caplog.at_level(logging.INFO):
        text_of(await world.call("search_drive", query="Tom's plan"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveMCP] tool=")]
    assert line.endswith(f" wide=0 batch=1 shown={SearchCaps.DEFAULT.about} more=1")
    assert "Tom's plan" not in line


async def test_other_tools_log_no_paging_fields(world, spans, caplog):
    with caplog.at_level(logging.INFO):
        text_of(await world.call("read_passages", file_ref=ref("d1"), passages="from 1"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveMCP] tool=")]
    assert " wide=" not in line and " more=" not in line
    assert "wide" not in spans[0].attributes


# -- the facade -------------------------------------------------------------------------------------------------------

async def test_the_facade_passes_wide_and_batch_only_when_given():
    recorder = Recorder()
    facade = DriveReadOperations(recorder)   # type: ignore[arg-type]
    await facade.search(CTX, "u1", query="Tom's plan", wide="yes")
    await facade.find_passages(CTX, "u1", question=QUESTION, wide="", batch="2")
    await facade.find_passages(CTX, "u1", question=QUESTION)
    (_, _, search_kw), (_, _, wide_kw), (_, _, plain_kw) = recorder.calls
    assert search_kw["wide"] == "yes"
    assert (wide_kw["wide"], wide_kw["batch"]) == ("", "2")
    assert "wide" not in plain_kw and "batch" not in plain_kw
