"""scripts/infrastructure/drive_initial_build_reset.py (USER_DRIVE_INDEXING_RFC §25.4.5, D27): the only way an initial
build reopens. Dry run by default; --apply writes an unopened build with the given allowance (INITIAL_BUILD_USD by
default, never under the owner's $20 minimum) in the user's `{user_id}:initial` usage document."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.domain.drive_enrichment import InitialBuild
from src.domain.drive_index_params import INITIAL_BUILD_USD

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_initial_build_reset.py"
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_initial_build_reset", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Store:
    def __init__(self, build=None):
        self.build = build
        self.writes = []

    async def get(self, user_id):
        return self.build

    async def put(self, user_id, build):
        self.writes.append((user_id, build))
        self.build = build


SPENT = InitialBuild(allowance_usd=40.0, spent_usd=40.0, opened_at=NOW, closed_at=NOW)


async def test_a_dry_run_says_what_it_would_write_and_writes_nothing(script):
    store = Store(SPENT)
    code, message = await script.reset(store, "u", allowance_usd=None, apply=False)
    assert code == 0 and "closed" in message and "--apply" in message and store.writes == []


async def test_apply_writes_an_unopened_build_with_the_default_allowance(script):
    store = Store(SPENT)
    code, message = await script.reset(store, "u", allowance_usd=None, apply=True)
    assert code == 0 and "reset" in message
    assert store.writes == [("u", InitialBuild(allowance_usd=INITIAL_BUILD_USD, spent_usd=0.0,
                                               opened_at=None, closed_at=None))]


async def test_apply_takes_an_explicit_allowance(script):
    store = Store(None)
    await script.reset(store, "u", allowance_usd=25.0, apply=True)
    assert store.build.allowance_usd == 25.0 and store.build.closed_at is None


async def test_an_allowance_under_the_owner_minimum_is_refused(script):
    store = Store(SPENT)
    code, message = await script.reset(store, "u", allowance_usd=5.0, apply=True)
    assert code == 1 and "20" in message and store.writes == []
