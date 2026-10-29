"""Startup I6 (main service): no start without the key configuration (SECRETS_AT_REST_RFC §2.6, §8 Startup; plan D-P1).

`build_application_graph` builds the cipher and the digest key before anything else, so a missing KMS key name or a
short, empty or non-hex digest key raises there, before any repository, service or blueprint exists, and `main()`
exits 1 (the graph build sits in its infrastructure try block). It never reaches the MCP build, whose errors `main()`
swallows. Both the graph and `main()` run over the sealed deploy environment (`tests/support/prod_env.py`).
"""
from typing import Any, List

import pytest

import main
from src.adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from src.composition import cipher_setup
from src.composition.application_graph import build_application_graph
from tests.support import prod_env

KEY_NAME = "projects/p-test/locations/l-test/keyRings/r-test/cryptoKeys/k-test"
GOOD_DIGEST_KEY = "ab" * 32
SHORT_DIGEST_KEY = "ab" * 31

BAD_CONFIGS = [
    pytest.param({"KMS_CRYPTO_KEY": "", "SECRET_DIGEST_KEY": GOOD_DIGEST_KEY}, "KMS_CRYPTO_KEY", id="empty-kms-key"),
    pytest.param({"KMS_CRYPTO_KEY": None, "SECRET_DIGEST_KEY": GOOD_DIGEST_KEY}, "KMS_CRYPTO_KEY", id="no-kms-key"),
    pytest.param({"KMS_CRYPTO_KEY": "  ", "SECRET_DIGEST_KEY": GOOD_DIGEST_KEY}, "KMS_CRYPTO_KEY", id="blank-kms-key"),
    pytest.param({"KMS_CRYPTO_KEY": KEY_NAME, "SECRET_DIGEST_KEY": ""}, "SECRET_DIGEST_KEY", id="empty-digest-key"),
    pytest.param({"KMS_CRYPTO_KEY": KEY_NAME, "SECRET_DIGEST_KEY": None}, "SECRET_DIGEST_KEY", id="no-digest-key"),
    pytest.param({"KMS_CRYPTO_KEY": KEY_NAME, "SECRET_DIGEST_KEY": SHORT_DIGEST_KEY}, "at least 32 bytes",
                 id="short-digest-key"),
    pytest.param({"KMS_CRYPTO_KEY": KEY_NAME, "SECRET_DIGEST_KEY": "zz" * 32}, "must be hex", id="non-hex-digest-key"),
]


class UntouchableDb:
    """A Firestore client the graph must not reach: any attribute access fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"build_application_graph touched the database ({name}) before refusing to start")


@pytest.mark.parametrize("values, message", BAD_CONFIGS)
def test_the_graph_refuses_to_build_before_touching_anything(monkeypatch, values, message):
    config, _ = prod_env.prod_settings(monkeypatch)
    config.update(values)

    with pytest.raises(ValueError, match=message):
        build_application_graph(config, UntouchableDb())


@pytest.mark.parametrize("values, message", BAD_CONFIGS)
async def test_main_exits_1_before_any_blueprint(monkeypatch, caplog, values, message):
    services = prod_env.install_offline_services(monkeypatch)
    for key, value in values.items():
        monkeypatch.setenv(key, value or "")
    monkeypatch.setattr(main, "init_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(main, "build_memory_telemetry", lambda *a, **k: None)
    blueprints: List[str] = []
    for name in dir(main):
        if name.startswith("create_") and name.endswith("_blueprint") or name in ("build_mcp_components",):
            monkeypatch.setattr(main, name, lambda *a, _n=name, **k: blueprints.append(_n))

    with pytest.raises(SystemExit) as exit_info:
        await main.main()

    assert exit_info.value.code == 1
    assert blueprints == []
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("Infrastructure Initialization Error" in m and message in m for m in errors), errors
    assert services.fallbacks == [] or set(services.fallbacks) <= {"KMS_CRYPTO_KEY", "SECRET_DIGEST_KEY"}


def test_valid_values_give_the_kms_cipher_and_the_key_bytes():
    cipher = cipher_setup.build_cipher(f" {KEY_NAME}\n")
    assert isinstance(cipher, GcpKmsCipherAdapter) and cipher._key_name == KEY_NAME
    assert cipher_setup.digest_key(GOOD_DIGEST_KEY + "\n") == bytes.fromhex(GOOD_DIGEST_KEY)
    assert cipher_setup.digest_key("cd" * 40) == bytes.fromhex("cd" * 40)


def test_load_settings_declares_both_keys(monkeypatch):
    config, _ = prod_env.prod_settings(monkeypatch)
    assert "KMS_CRYPTO_KEY" in config and "SECRET_DIGEST_KEY" in config


def test_the_main_service_deploy_mounts_both_secrets():
    assert {"KMS_CRYPTO_KEY", "SECRET_DIGEST_KEY"} <= set(prod_env.secret_names())
