"""`build_drive_mcp_or_none` forwards every argument to the drive connector's builder (mutation P4 survivors).

A dropped (`None`) authorization service, tool list or voice-token verifier is silent at boot: the build succeeds and the
connector then refuses every call, lists no tool, or refuses every voice MCP token.
"""
import main


def _capture(monkeypatch):
    seen = []
    app = object()

    def fake_build(**kwargs):
        seen.append(kwargs)
        return app

    monkeypatch.setattr(main, "build_drive_mcp_app", fake_build)
    return seen, app


def test_the_builder_gets_the_very_objects_passed_in(monkeypatch):
    seen, app = _capture(monkeypatch)
    authorization, auth_config, tools, voice_tokens = object(), object(), [object()], object()

    result = main.build_drive_mcp_or_none(authorization, auth_config, tools=tools, voice_tokens=voice_tokens)

    assert result is app
    [kwargs] = seen
    assert kwargs["authorization_service"] is authorization
    assert kwargs["auth_config"] is auth_config
    assert kwargs["tools"] is tools
    assert kwargs["voice_tokens"] is voice_tokens


def test_a_failing_build_yields_none(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "build_drive_mcp_app", boom)

    assert main.build_drive_mcp_or_none(object(), object()) is None
