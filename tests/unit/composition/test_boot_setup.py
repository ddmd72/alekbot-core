"""`build_boot_checks`: the boot values in their fail-closed order, each forwarded into the bundle as built."""
import dataclasses

import pytest

from src.composition import boot_setup
from src.composition.boot_setup import BootChecks, build_boot_checks


class _Auth:
    def __init__(self, config):
        self.config = config

    def validate(self):
        _steps.append("auth")


_steps: list = []


@pytest.fixture
def recorded(monkeypatch):
    _steps.clear()
    made = {}

    def step(name, value):
        def build(*args, **kwargs):
            _steps.append(name)
            made[name] = (args, kwargs)
            return value
        return build

    monkeypatch.setattr(boot_setup.cipher_setup, "build_cipher", step("cipher", "CIPHER"))
    monkeypatch.setattr(boot_setup.cipher_setup, "digest_key", step("digest", b"DIGEST"))
    monkeypatch.setattr(boot_setup.voice_mcp_setup, "signing_key", step("voice", "VOICE-KEY"))
    monkeypatch.setattr(boot_setup.voice_mcp_setup, "effective_oauth_secret", lambda config: "OAUTH-SECRET")
    monkeypatch.setattr(boot_setup.voice_mcp_setup, "voice_mcp_tools_enabled", lambda: True)
    monkeypatch.setattr(boot_setup, "AuthConfig", _Auth)
    monkeypatch.setattr(boot_setup, "build_deploy_topology", step("topology", "TOPOLOGY"))
    monkeypatch.setattr(boot_setup, "build_sandbox_parser", step("parser", "PARSER"))
    monkeypatch.setattr(boot_setup, "build_sandbox_media", step("media", "MEDIA"))
    return made


def test_the_checks_run_in_their_fail_closed_order_and_fill_the_bundle(recorded):
    config = {"KMS_CRYPTO_KEY": "kms-key-1", "SECRET_DIGEST_KEY": "digest-1", "VOICE_MCP_SIGNING_KEY": "voice-1",
              "SANDBOX_PARSE_URL": "https://sandbox.example.test"}
    env_config = object()

    boot = build_boot_checks(config, env_config)

    assert _steps == ["cipher", "digest", "voice", "auth", "topology", "parser", "media"]
    assert (boot.cipher, boot.digest_key, boot.voice_mcp_signing_key) == ("CIPHER", b"DIGEST", "VOICE-KEY")
    assert boot.auth_config.config is config
    assert (boot.deploy_topology, boot.parse_adapter, boot.video_duration) == ("TOPOLOGY", "PARSER", "MEDIA")
    assert recorded["cipher"] == (("kms-key-1",), {}) and recorded["digest"] == (("digest-1",), {})
    assert recorded["voice"] == (("voice-1",), {"oauth_secret": "OAUTH-SECRET", "tools_enabled": True})
    assert recorded["topology"] == ((config, env_config), {})
    assert recorded["parser"] == (("https://sandbox.example.test",), {})
    assert recorded["media"] == (("https://sandbox.example.test",), {})


def test_without_a_sandbox_url_both_sandbox_builders_get_the_empty_url_and_refuse_there(recorded):
    build_boot_checks({}, object())

    assert recorded["parser"] == (("",), {}) and recorded["media"] == (("",), {})


def test_a_refusal_stops_the_later_checks(recorded, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("bad digest key")
    monkeypatch.setattr(boot_setup.cipher_setup, "digest_key", refuse)

    with pytest.raises(RuntimeError, match="bad digest key"):
        build_boot_checks({}, object())

    assert _steps == ["cipher"]


def test_the_keys_stay_out_of_the_repr_and_the_bundle_is_frozen():
    boot = BootChecks(cipher="C", digest_key=b"secret-digest", voice_mcp_signing_key="secret-voice", auth_config=None,
                      deploy_topology=None, parse_adapter=None, video_duration=None)  # type: ignore[arg-type]

    assert "secret-digest" not in repr(boot) and "secret-voice" not in repr(boot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        boot.cipher = "other"  # type: ignore[misc]
