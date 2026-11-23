"""`main()` hands the drive connector and the shared ASGI app the real objects (mutation survivors, main.py MCP block).

A `None` for the quota service, the authorization service, the drive tools, the voice tokens or the drive app is silent at
boot and bites only when a drive tool runs, a token is checked or `/drive-mcp` is requested.
"""
from typing import Any, Dict, List

import pytest

import main
from tests.unit.test_main_wiring_values import Captured, _instrumented_boot

pytestmark = pytest.mark.slow


@pytest.fixture
async def drive_builds(monkeypatch, caplog) -> tuple:
    tool_calls: List[Dict[str, Any]] = []
    tools_built: List[Any] = []
    mcp_calls: List[tuple] = []
    mcp_built: List[Any] = []
    asgi_calls: List[Dict[str, Any]] = []
    voice_built: List[Any] = []
    tools_real = main.build_drive_connector_tools_from_container
    mcp_real = main.build_drive_mcp_or_none
    asgi_real = main.build_shared_asgi
    voice_real = main.build_voice_mcp_tokens

    async def tools(container: Any, **kwargs: Any) -> Any:
        tool_calls.append(kwargs)
        built = await tools_real(container, **kwargs)
        tools_built.append(built)
        return built

    def drive_mcp(*args: Any, **kwargs: Any) -> Any:
        mcp_calls.append((args, kwargs))
        built = mcp_real(*args, **kwargs)
        mcp_built.append(built)
        return built

    def asgi(*args: Any, **kwargs: Any) -> Any:
        asgi_calls.append(kwargs)
        return asgi_real(*args, **kwargs)

    def voice(**kwargs: Any) -> Any:
        built = voice_real(**kwargs)
        voice_built.append(built)
        return built

    monkeypatch.setattr(main, "build_drive_connector_tools_from_container", tools)
    monkeypatch.setattr(main, "build_drive_mcp_or_none", drive_mcp)
    monkeypatch.setattr(main, "build_shared_asgi", asgi)
    monkeypatch.setattr(main, "build_voice_mcp_tokens", voice)
    booted: Captured = await _instrumented_boot(monkeypatch, caplog)
    return booted, tool_calls, tools_built, mcp_calls, mcp_built, asgi_calls, voice_built


async def test_the_drive_tools_get_the_graphs_quota_service(drive_builds):
    booted, tool_calls, *_ = drive_builds
    [kwargs] = tool_calls

    assert booted.app.quota_service is not None
    assert kwargs["quota"] is booted.app.quota_service


async def test_the_drive_app_gets_the_authorization_service_tools_and_voice_tokens(drive_builds):
    booted, _, tools_built, mcp_calls, _, _, voice_built = drive_builds
    [(args, kwargs)] = mcp_calls
    [(mcp_kwargs, components)] = booted.mcp_builds

    assert components.authorization_service is not None
    assert args[0] is components.authorization_service
    assert tools_built[0] is not None
    assert kwargs["tools"] is tools_built[0]
    assert voice_built[0] is not None
    assert kwargs["voice_tokens"] is voice_built[0]


async def test_the_shared_asgi_app_gets_the_drive_app(drive_builds):
    _, _, _, _, mcp_built, asgi_calls, _ = drive_builds
    [kwargs] = asgi_calls

    assert mcp_built[0] is not None
    assert kwargs["drive_mcp"] is mcp_built[0]
