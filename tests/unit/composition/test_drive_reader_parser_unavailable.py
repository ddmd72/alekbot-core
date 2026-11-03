"""ParserUnavailable inside a reader run is systemic: it is raised from `extract` (never the `ERROR` outcome of an
unexpected window failure), carrying what the finished windows spent, in both the indexer path and the limited
(interactive) path. At the page count it is raised with a WARNING line."""
import logging
from dataclasses import replace

import pytest

from src.domain.drive_extraction import ParserUnavailable
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner_halving import _Cutter, _key, _request
from tests.unit.composition.test_drive_reader_runner_halving import _runner as _plain_runner


class _DownCutter(_Cutter):
    """Cuts fine until the cut that starts at `down_from`, where the sandbox is unavailable."""

    def __init__(self, pages_total, down_from=None, count_down=False):
        super().__init__(pages_total)
        self.down_from = down_from
        self.count_down = count_down

    async def pdf_page_count(self, path, *, deadline_at=None):
        if self.count_down:
            raise ParserUnavailable("unavailable")
        return self.pages_total

    async def cut_pdf_window(self, path, first_page, page_count, out_path, *, deadline_at=None):
        if first_page == self.down_from:
            raise ParserUnavailable("misconfigured")
        await super().cut_pdf_window(path, first_page, page_count, out_path)


def _runner(cutter, pages_total=25):
    runner, llm, _ = _plain_runner(lambda r: reply({"text": f"text of {_key(r)}", "brief": None}), pages_total)
    runner._splitter = cutter
    return runner, llm


async def test_a_page_count_outage_is_raised_with_a_warning_not_an_error_outcome(tmp_path, caplog):
    runner, llm = _runner(_DownCutter(25, count_down=True))
    with caplog.at_level(logging.INFO), pytest.raises(ParserUnavailable) as raised:
        await runner.extract(_request(tmp_path))
    assert raised.value.reason == "unavailable" and llm.requests == []
    assert any(r.levelno == logging.WARNING and "parser is unavailable" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_a_window_cut_outage_in_the_indexer_path_is_raised_with_the_spend(tmp_path, caplog):
    runner, llm = _runner(_DownCutter(25, down_from=10))
    with caplog.at_level(logging.INFO), pytest.raises(ParserUnavailable) as raised:
        await runner.extract(_request(tmp_path))
    assert raised.value.reason == "misconfigured" and raised.value.charges_file is False
    assert raised.value.model_calls == len(llm.requests)
    assert not any("window failed unexpectedly" in r.getMessage() for r in caplog.records)


async def test_a_window_cut_outage_in_the_limited_path_is_raised_with_the_spend(tmp_path, caplog):
    runner, llm = _runner(_DownCutter(25, down_from=10))
    request = replace(_request(tmp_path), text_budget=10_000)
    with caplog.at_level(logging.INFO), pytest.raises(ParserUnavailable) as raised:
        await runner.extract(request)
    assert raised.value.reason == "misconfigured"
    assert raised.value.model_calls == len(llm.requests) == 1 and raised.value.pages_sent == 10
    assert not any("window failed unexpectedly" in r.getMessage() for r in caplog.records)
