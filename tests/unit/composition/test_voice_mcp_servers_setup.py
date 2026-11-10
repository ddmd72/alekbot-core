"""`voice_mcp_servers`: the VOICE_MCP_TOOLS knob gives LelikAgent the drive server descriptor at the canonical resource
URI of the deployment's `MCP_RESOURCE_URI`, never a hardcoded host (LELIK_VOICE_MCP_RFC §4, S3; memory goes by
delegation since the §2 amendment of 2026-11-10)."""
import pytest

from src.composition.voice_mcp_setup import voice_mcp_servers


@pytest.mark.parametrize("knob", ["on", " ON "])
def test_the_knob_on_gives_the_drive_server_at_the_deployments_resource_uri(monkeypatch, knob):
    monkeypatch.setenv("VOICE_MCP_TOOLS", knob)

    servers = voice_mcp_servers({"MCP_RESOURCE_URI": "https://mcp.example.test/mcp"})

    assert [(s.label, s.url, s.token) for s in servers] == [
        ("alek_drive", "https://mcp.example.test/drive-mcp", ""),
    ]


@pytest.mark.parametrize("knob", [None, "", "off", "true", "1"])
def test_anything_but_on_gives_none(monkeypatch, knob):
    if knob is None:
        monkeypatch.delenv("VOICE_MCP_TOOLS", raising=False)
    else:
        monkeypatch.setenv("VOICE_MCP_TOOLS", knob)

    assert voice_mcp_servers({"MCP_RESOURCE_URI": "https://mcp.example.test/mcp"}) == ()
