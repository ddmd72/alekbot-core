"""`main()` hands the voice MCP token service the graph's repositories, the digest key and the one voice ticket store (mutation P4).

A `None` for any of these is silent at boot and bites only when a voice token is minted or verified: the live-call
records would have no store, token digests no key, the owner lookup no user repository, the whitelist re-check no
repository, and a refusal would raise no alert.
"""
from typing import Any, Dict, List

import pytest

import main
from tests.unit.test_main_wiring_values import Captured, _instrumented_boot

pytestmark = pytest.mark.slow


@pytest.fixture
async def token_build(monkeypatch, caplog) -> tuple:
    calls: List[Dict[str, Any]] = []
    real = main.build_voice_mcp_tokens

    def recording(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(main, "build_voice_mcp_tokens", recording)
    booted: Captured = await _instrumented_boot(monkeypatch, caplog)
    [kwargs] = calls
    return booted, kwargs


async def test_the_voice_token_service_gets_the_one_voice_ticket_store(token_build):
    booted, kwargs = token_build
    [(_, _, store)] = booted.stores

    assert kwargs["store"] is store


async def test_the_voice_token_service_gets_the_graphs_digest_key(token_build):
    booted, kwargs = token_build

    assert booted.app.digest_key
    assert kwargs["digest_key"] is booted.app.digest_key


async def test_the_voice_token_service_gets_the_graphs_user_and_whitelist_repositories(token_build):
    booted, kwargs = token_build

    assert booted.app.user_repo is not None
    assert booted.app.whitelist_repo is not None
    assert kwargs["user_repo"] is booted.app.user_repo
    assert kwargs["whitelist_repo"] is booted.app.whitelist_repo


async def test_the_voice_token_service_gets_the_graphs_alert_sink(token_build):
    booted, kwargs = token_build

    assert booted.app.alert_sink is not None
    assert kwargs["alert_sink"] is booted.app.alert_sink
