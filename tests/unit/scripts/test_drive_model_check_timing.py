"""The owner's live model check records per-window timings and output speed (C17; adversarial review C-M2): every
reader call's seconds, output tokens and tokens/s, and per provider the slowest call against the configured
per-window timeout — the numbers that calibrate it. Offline: fake providers, prompts from upload files."""
import asyncio
import json
from pathlib import Path

import pytest

from scripts.validation import drive_model_check as check
from src.domain.exceptions import LLMRateLimitError
from src.infrastructure.agent_config import DRIVE_READER
from src.services.provider_registry import ProviderRegistry
from tests.unit.agents.drive_model_fakes import RoutedLLM, file_bytes, reply


def _uploads(tmp_path: Path) -> Path:
    """Prompt upload files for both drive agents (tokens, blueprint, profile), as firestore_utils/uploads holds."""
    up = tmp_path / "uploads"
    up.mkdir()
    for agent in ("drive_reader", "drive_triage"):
        tag = agent.split("_")[1].upper()
        for cls in ("cognitive_process", "output_format"):
            tid = f"{cls.upper()}_DRIVE_{tag}"
            (up / f"{tid}.json").write_text(json.dumps({"token_id": tid, "category": cls, "class": cls,
                                                        "content": f'rules: "{tid}"', "metadata": {}}))
        (up / f"{agent}_agent_v1.json").write_text(json.dumps({
            "blueprint_id": f"{agent}_agent_v1", "outer_class": "A extends Agent",
            "class_order": ["cognitive_process", "output_format"]}))
        (up / f"{agent}.json").write_text(json.dumps({"agent_id": agent, "blueprint_id": f"{agent}_agent_v1",
                                                      "tokens": {f"COGNITIVE_PROCESS_DRIVE_{tag}": {"order": 10},
                                                                 f"OUTPUT_FORMAT_DRIVE_{tag}": {"order": 10}}}))
    return up


class _Cutter:
    async def pdf_page_count(self, path):
        return 25

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        Path(out_path).write_bytes(f"{Path(path).name}:{first_page}".encode())


def _registry(handler, *providers):
    registry = ProviderRegistry()
    for name in providers:
        registry.register(name, RoutedLLM(handler))
    return registry


async def test_each_window_is_timed_with_its_output_speed(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF")

    async def handler(request):
        await asyncio.sleep(0.02)
        return reply({"text": "x", "brief": None}, completion_tokens=400)

    out = await check.run(["read", str(folder), "--providers", "claude,openai"], registry=_registry(handler, "claude"),
                          assembly=check.local_assembly_service(_uploads(tmp_path)), splitter=_Cutter(),
                          memory=tmp_path / "memory")
    summary = json.loads((out / "read" / "summary.json").read_text())
    assert set(summary) == {"claude"}   # openai has no key here: skipped, not a crash
    (row,) = summary["claude"]["files"]
    windows = row["windows"]
    assert sorted(w["window"] for w in windows) == ["window-001.pdf", "window-002.pdf", "window-003.pdf"]
    assert all(w["output_tokens"] == 400 and w["seconds"] >= 0.02 and w["tokens_per_s"] > 0 for w in windows)
    assert all(w["timeout_s"] == DRIVE_READER.call_timeout_s for w in windows)
    timing = summary["claude"]["timing"]
    assert timing["calls"] == 3 and timing["timeout_s"] == DRIVE_READER.call_timeout_s
    assert timing["max_seconds"] >= 0.02 and timing["median_tokens_per_s"] > 0
    assert timing["max_share_of_timeout"] == round(timing["max_seconds"] / DRIVE_READER.call_timeout_s, 2)


class _WindowTasks:
    """The window tasks the reader runner started, keyed by first page. The provider is called inside the window's own
    task, so a handler can record it, and another window can wait until it is done — counted by the runner — before
    answering. The runner runs windows concurrently and cancels the rest at the first outage, counting only what
    answered (§24.7), so which siblings a 429 leaves counted is a scheduling race unless the test orders it."""

    def __init__(self) -> None:
        self.tasks: dict = {}
        self.seen = asyncio.Event()

    def record(self, first_page: str, expected: int) -> None:
        self.tasks[first_page] = asyncio.current_task()
        if len(self.tasks) >= expected:
            self.seen.set()

    async def finished(self, *first_pages: str) -> None:
        await asyncio.wait_for(self.seen.wait(), timeout=10)
        await asyncio.wait_for(asyncio.wait([self.tasks[p] for p in first_pages]), timeout=10)


async def test_an_outage_row_keeps_its_partial_usage_and_failed_call(tmp_path):
    """The 429 window answers only after both other windows were counted: the row carries their 20 pages and all
    three calls (the failed one included)."""
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF")
    windows = _WindowTasks()

    async def handler(request):
        first_page = file_bytes(request).decode().rsplit(":", 1)[1]
        if first_page == "20":
            await windows.finished("0", "10")
            return LLMRateLimitError("429", http_status=429)
        windows.record(first_page, expected=2)
        return reply({"text": "x", "brief": None})

    out = await check.run(["read", str(folder), "--providers", "claude"], registry=_registry(handler, "claude"),
                          assembly=check.local_assembly_service(_uploads(tmp_path)), splitter=_Cutter(),
                          memory=tmp_path / "memory")
    (row,) = json.loads((out / "read" / "summary.json").read_text())["claude"]["files"]
    assert row["status"] == "model_unavailable" and row["pages_sent"] == 20 and row["calls"] == 3
    assert [w.get("error") for w in row["windows"]].count("LLMRateLimitError") == 1


async def test_an_outage_cancels_a_window_still_in_flight_and_does_not_count_it(tmp_path):
    """The other order: the 429 lands while window 2 is still waiting for its answer. Window 2 is cancelled and, never
    answered, is not counted (§24.7): the row carries window 1's 10 pages and two calls (window 1's and the 429)."""
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF")
    windows = _WindowTasks()
    cancelled = []

    async def handler(request):
        first_page = file_bytes(request).decode().rsplit(":", 1)[1]
        if first_page == "20":
            await windows.finished("0")
            return LLMRateLimitError("429", http_status=429)
        windows.record(first_page, expected=2)
        if first_page == "10":
            try:   # answers only if the runner failed to cancel it: then the assertions below fail, not hang
                await asyncio.wait_for(asyncio.Event().wait(), timeout=30)
            except asyncio.CancelledError:
                cancelled.append(first_page)
                raise
            except asyncio.TimeoutError:
                pass
        return reply({"text": "x", "brief": None})

    out = await check.run(["read", str(folder), "--providers", "claude"], registry=_registry(handler, "claude"),
                          assembly=check.local_assembly_service(_uploads(tmp_path)), splitter=_Cutter(),
                          memory=tmp_path / "memory")
    (row,) = json.loads((out / "read" / "summary.json").read_text())["claude"]["files"]
    assert cancelled == ["10"]
    assert row["status"] == "model_unavailable" and row["pages_sent"] == 10 and row["calls"] == 2
    assert sorted(w["window"] for w in row["windows"]) == ["window-001.pdf", "window-003.pdf"]
    assert [w.get("error") for w in row["windows"]].count("LLMRateLimitError") == 1


def test_timing_summary_without_calls():
    assert check.timing_summary([])["max_seconds"] is None


@pytest.mark.parametrize("seconds,share", [(90.0, 0.5), (180.0, 1.0)])
def test_share_of_the_timeout(seconds, share):
    calls = [{"seconds": seconds, "tokens_per_s": 50.0, "timeout_s": 180}]
    assert check.timing_summary(calls)["max_share_of_timeout"] == share
