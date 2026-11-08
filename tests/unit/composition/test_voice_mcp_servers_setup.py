"""`voice_mcp_servers`: the VOICE_MCP_TOOLS knob gives LelikAgent the memory and drive server descriptors at the
canonical resource URIs of the deployment's `MCP_RESOURCE_URI`, never a hardcoded host (LELIK_VOICE_MCP_RFC §4, S3)."""
import pytest

from src.composition.voice_mcp_setup import voice_mcp_servers


@pytest.mark.parametrize("knob", ["on", " ON "])
def test_the_knob_on_gives_both_servers_at_the_deployments_resource_uris(monkeypatch, knob):
    monkeypatch.setenv("VOICE_MCP_TOOLS", knob)

    servers = voice_mcp_servers({"MCP_RESOURCE_URI": "https://mcp.example.test/mcp"})

    assert [(s.label, s.url, s.token) for s in servers] == [
        ("alek_memory", "https://mcp.example.test/mcp", ""),
        ("alek_drive", "https://mcp.example.test/drive-mcp", ""),
    ]


@pytest.mark.parametrize("knob", [None, "", "off", "true", "1"])
def test_anything_but_on_gives_none(monkeypatch, knob):
    if knob is None:
        monkeypatch.delenv("VOICE_MCP_TOOLS", raising=False)
    else:
        monkeypatch.setenv("VOICE_MCP_TOOLS", knob)

    assert voice_mcp_servers({"MCP_RESOURCE_URI": "https://mcp.example.test/mcp"}) == ()
