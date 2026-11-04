"""DRIVE_MCP_CONNECTOR_RFC §14.8, §5.3: a token may run ahead of the revision that reads it. An unknown key is not a
problem: the texts load without it and the loader names it in one WARNING (once per set of unknown keys, not on every
refresh); a missing key still fails, and a misspelled key fails as the missing one."""
import logging

import pytest

from src.domain.connector_texts import ConnectorTexts
from src.domain.exceptions import ConnectorTextsUnavailable
from src.services.connector_text_service import ConnectorTextService
from tests.unit.services.test_connector_text_service import (
    RESULT_BODY, RESULT_ID, RESULT_KEYS, TOOL_BODY, TOOL_ID, TOOL_KEYS, FakeClock, FakeTokenRepo, make_token,
)

AHEAD_TOOL = TOOL_BODY + "### find_files.wide\nMore files about the Hamburg conference.\n"
AHEAD_RESULT = RESULT_BODY + "### BATCH_END\nNo further passages on policy MP-2026-118204.\n"


# -- the domain parse -------------------------------------------------------------------------------------------------

def test_unknown_keys_are_returned_apart_and_left_out_of_the_texts():
    texts, unknown = ConnectorTexts.parse_bodies(AHEAD_TOOL, AHEAD_RESULT, TOOL_KEYS, RESULT_KEYS)
    assert unknown == {"tools:find_files.wide", "results:BATCH_END"}
    assert set(texts.tools) == TOOL_KEYS and set(texts.results) == RESULT_KEYS
    assert ConnectorTexts.from_bodies(AHEAD_TOOL, AHEAD_RESULT, TOOL_KEYS, RESULT_KEYS) == texts


def test_a_body_with_exactly_the_required_keys_has_no_unknown_key():
    assert ConnectorTexts.parse_bodies(TOOL_BODY, RESULT_BODY, TOOL_KEYS, RESULT_KEYS)[1] == frozenset()


def test_a_misspelled_key_still_fails_as_the_missing_one():
    body = TOOL_BODY.replace("### find_files.query", "### find_files.qeury")
    with pytest.raises(ValueError, match="tool texts: missing keys find_files.query"):
        ConnectorTexts.parse_bodies(body, RESULT_BODY, TOOL_KEYS, RESULT_KEYS)


# -- the service ------------------------------------------------------------------------------------------------------

def _service(tool_body, result_body, clock=None):
    repo = FakeTokenRepo({TOOL_ID: make_token(TOOL_ID, tool_body), RESULT_ID: make_token(RESULT_ID, result_body)})
    return ConnectorTextService(repo, tool_token_id=TOOL_ID, result_token_id=RESULT_ID, required_tool_keys=TOOL_KEYS,
                                required_result_keys=RESULT_KEYS, clock=clock or FakeClock()), repo


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
            and r.getMessage().startswith("[ConnectorTexts]")]


async def test_an_unknown_key_loads_with_one_warning_naming_it(caplog):
    service, _ = _service(AHEAD_TOOL, AHEAD_RESULT)
    with caplog.at_level(logging.WARNING):
        texts = await service.texts()
    assert set(texts.tools) == TOOL_KEYS and set(texts.results) == RESULT_KEYS
    (line,) = _warnings(caplog)
    assert "results:BATCH_END" in line and "tools:find_files.wide" in line
    assert "MP-2026-118204" not in line   # the key, never its text


async def test_the_warning_is_not_repeated_on_every_refresh_but_on_a_new_set(caplog):
    clock = FakeClock()
    service, repo = _service(AHEAD_TOOL, AHEAD_RESULT, clock)
    with caplog.at_level(logging.WARNING):
        await service.texts()
        clock.now += 61
        await service.texts()
        assert len(_warnings(caplog)) == 1
        repo.tokens[RESULT_ID] = make_token(RESULT_ID, RESULT_BODY + "### SEARCH_MORE\nNarrow to the shed folder.\n")
        clock.now += 61
        await service.texts()
    assert len(_warnings(caplog)) == 2 and "results:SEARCH_MORE" in _warnings(caplog)[1]


async def test_a_missing_key_still_leaves_the_texts_unloaded():
    service, _ = _service(AHEAD_TOOL, "### BATCH_END\nNo further passages.\n")
    with pytest.raises(ConnectorTextsUnavailable, match="missing keys R_FIND"):
        await service.texts()
