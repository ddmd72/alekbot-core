"""The routing probe has a hard cost stop (cross-cut review R3): `--max-usd` is required, like every paid eval."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.validation import drive_routing_probe as probe  # noqa: E402


def test_max_usd_is_required():
    with pytest.raises(SystemExit):
        probe.build_parser().parse_args(["--runs", "1"])


def test_max_usd_and_runs_are_parsed():
    args = probe.build_parser().parse_args(["--runs", "2", "--max-usd", "3"])
    assert (args.runs, args.max_usd) == (2, 3.0)


async def test_the_patched_loader_and_the_meter_are_restored_when_a_run_raises(monkeypatch):
    """M5: a raising run must not leave the loader patched or the meter installed."""
    from types import SimpleNamespace

    from src.infrastructure.delegation_engine import DelegationEngine

    monkeypatch.setattr(DelegationEngine, "dispatch", DelegationEngine.dispatch)  # restored after the test

    async def original(*a, **k):
        return []

    async def boom(msg):
        raise RuntimeError("provider down")

    smart = SimpleNamespace(agent_id="smart", _load_conversation_context=original, execute=boom)
    uninstalled = []
    meter = SimpleNamespace(spent=0.0, capped=False, install=lambda: lambda: uninstalled.append(True))

    with pytest.raises(RuntimeError):
        await probe.run_scenarios(smart, meter, "u", "a", 1, 1.0)

    assert smart._load_conversation_context is original
    assert uninstalled == [True]
