"""`main()` forwards the graph's cipher and digest key to the voice call setup and the MCP components (mutation P3 survivors).

A `None` there is silent at boot and bites only on a request: the call-setup ticket keys would be derived from no key, and the
MCP client repository would store and look up secrets without the cipher or the digest.
"""
import pytest

from tests.unit.test_main_wiring_values import booted  # noqa: F401  (the shared fixture)

pytestmark = pytest.mark.slow


async def test_the_voice_call_setup_gets_the_graphs_digest_key(booted):
    [(_, kwargs, _setup)] = booted.setups

    assert booted.app.digest_key
    assert kwargs["digest_key"] is booted.app.digest_key


async def test_the_mcp_components_get_the_graphs_cipher_and_digest_key(booted):
    [(build_kwargs, _components)] = booted.mcp_builds

    assert booted.app.cipher is not None
    assert booted.app.digest_key
    assert build_kwargs["cipher"] is booted.app.cipher
    assert build_kwargs["digest_key"] is booted.app.digest_key
