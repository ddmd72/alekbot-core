"""ConnectorTextService: load, validate, cache, fail loudly (no inline default text)."""
import asyncio
import logging
from typing import Dict, List, Optional

import pytest

from src.domain.exceptions import ConnectorTextsUnavailable
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.services.connector_text_service import ConnectorTextService

TOOL_ID = "MCP_DRIVE_TOOL_TEXTS"
RESULT_ID = "MCP_DRIVE_RESULT_TEXTS"
TOOL_KEYS = {"find_files", "find_files.query"}
RESULT_KEYS = {"R_FIND"}
TOOL_BODY = "### find_files\nFind files such as R&D/Tom's plan.docx.\n### find_files.query\nWords to look for.\n"
RESULT_BODY = "### R_FIND\nFound 3 files for invoice FA-0137.\n"


def make_token(token_id: str, content: str, risk: Optional[str] = "safe") -> Token:
    metadata: Dict = {} if risk is None else {"validation": {"risk_level": risk}}
    return Token(id=TokenId(token_id), category=TokenCategory("connector"), class_=TokenClass("properties"),
                 content=content, metadata=metadata)


class FakeTokenRepo:
    """Duck-typed stand-in for TokenRepository: only `get` is used by the service."""

    def __init__(self, tokens: Dict[str, Token]) -> None:
        self.tokens = tokens
        self.reads: List[str] = []
        self.delay = 0.0

    async def get(self, token_id: TokenId) -> Token:
        self.reads.append(str(token_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.tokens[str(token_id)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeAlertSink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


def good_repo() -> FakeTokenRepo:
    return FakeTokenRepo({TOOL_ID: make_token(TOOL_ID, TOOL_BODY), RESULT_ID: make_token(RESULT_ID, RESULT_BODY)})


def make_service(repo: FakeTokenRepo, clock: Optional[FakeClock] = None, sink: Optional[FakeAlertSink] = None):
    return ConnectorTextService(repo, tool_token_id=TOOL_ID, result_token_id=RESULT_ID,
                                required_tool_keys=TOOL_KEYS, required_result_keys=RESULT_KEYS, ttl_s=60.0,
                                clock=clock or FakeClock(), alert_sink=sink)  # type: ignore[arg-type]


async def test_first_load_returns_parsed_texts():
    texts = await make_service(good_repo()).texts()
    assert texts.tools["find_files"] == "Find files such as R&D/Tom's plan.docx."
    assert texts.results["R_FIND"] == "Found 3 files for invoice FA-0137."


async def test_low_risk_token_is_accepted():
    repo = good_repo()
    repo.tokens[TOOL_ID] = make_token(TOOL_ID, TOOL_BODY, risk="low")
    assert (await make_service(repo).texts()).tools["find_files.query"] == "Words to look for."


async def test_cache_hit_within_ttl_reads_nothing():
    repo, clock = good_repo(), FakeClock()
    service = make_service(repo, clock)
    first = await service.texts()
    clock.now += 59
    assert await service.texts() is first
    assert len(repo.reads) == 2


async def test_reload_after_ttl_sees_new_text():
    repo, clock = good_repo(), FakeClock()
    service = make_service(repo, clock)
    await service.texts()
    repo.tokens[RESULT_ID] = make_token(RESULT_ID, "### R_FIND\nFound 5 files.\n")
    clock.now += 60
    assert (await service.texts()).results["R_FIND"] == "Found 5 files."
    assert len(repo.reads) == 4


async def test_concurrent_calls_during_one_refresh_read_once():
    repo = good_repo()
    repo.delay = 0.01
    service = make_service(repo)
    results = await asyncio.gather(*(service.texts() for _ in range(5)))
    assert all(r is results[0] for r in results)
    assert len(repo.reads) == 2


async def test_missing_token_on_first_load_raises_and_alerts(caplog):
    repo = good_repo()
    del repo.tokens[RESULT_ID]
    sink = FakeAlertSink()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ConnectorTextsUnavailable, match=RESULT_ID):
            await make_service(repo, sink=sink).texts()
    assert any(RESULT_ID in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert len(sink.posts) == 1 and RESULT_ID in sink.posts[0]


async def test_failed_first_load_is_retried_and_alerts_each_time():
    repo = good_repo()
    del repo.tokens[TOOL_ID]
    sink = FakeAlertSink()
    service = make_service(repo, sink=sink)
    with pytest.raises(ConnectorTextsUnavailable):
        await service.texts()
    repo.tokens[TOOL_ID] = make_token(TOOL_ID, TOOL_BODY)
    assert (await service.texts()).tools["find_files"]
    assert len(sink.posts) == 1


@pytest.mark.parametrize("body, needle", [
    ("### find_files\nFind files.\n", "find_files.query"),
    (TOOL_BODY + "### extra\nNo.\n", "extra"),
])
async def test_missing_or_unknown_key_raises(body, needle):
    repo = good_repo()
    repo.tokens[TOOL_ID] = make_token(TOOL_ID, body)
    with pytest.raises(ConnectorTextsUnavailable, match=needle):
        await make_service(repo).texts()


@pytest.mark.parametrize("risk", ["medium", None])
async def test_non_clean_risk_raises_naming_token(risk):
    repo = good_repo()
    repo.tokens[RESULT_ID] = make_token(RESULT_ID, RESULT_BODY, risk=risk)
    with pytest.raises(ConnectorTextsUnavailable, match=RESULT_ID):
        await make_service(repo).texts()


async def test_failed_refresh_keeps_last_good_value_and_backs_off_for_ttl(caplog):
    repo, clock, sink = good_repo(), FakeClock(), FakeAlertSink()
    service = make_service(repo, clock, sink)
    good = await service.texts()
    del repo.tokens[TOOL_ID]
    clock.now += 61
    reads_before = len(repo.reads)
    with caplog.at_level(logging.ERROR):
        assert await service.texts() is good
        reads_after_failure = len(repo.reads)
        clock.now += 59
        assert await service.texts() is good
        assert len(repo.reads) == reads_after_failure
        assert reads_after_failure > reads_before
        clock.now += 1
        assert await service.texts() is good
        assert len(repo.reads) > reads_after_failure
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and TOOL_ID in r.getMessage()]
    assert len(errors) == 2
    assert not sink.posts


async def test_recovery_after_failed_refresh():
    repo, clock = good_repo(), FakeClock()
    service = make_service(repo, clock)
    await service.texts()
    saved = repo.tokens.pop(RESULT_ID)
    clock.now += 61
    await service.texts()
    repo.tokens[RESULT_ID] = make_token(RESULT_ID, "### R_FIND\nFound 7 files.\n")
    clock.now += 61
    assert (await service.texts()).results["R_FIND"] == "Found 7 files."
    assert saved is not None
