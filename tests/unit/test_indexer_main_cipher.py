"""Startup I6 on the indexer (SECRETS_AT_REST_RFC §2.6): `indexer_main.build_app` reads `KMS_CRYPTO_KEY` from its
environment (it has no `load_settings()`) and refuses to start without it, before any client is built; with it, the
drive's credential store holds the KMS cipher over that key name, never a cipher of its own."""
from unittest.mock import MagicMock

import pytest

import indexer_main
from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.adapters.gcp_kms_cipher_adapter import GcpKmsCipherAdapter
from src.composition import drive_indexing_setup

KEY_NAME = "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a"


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def env(monkeypatch):
    built = {"firestore": 0, "drive": None}
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("KMS_CRYPTO_KEY", raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "sa@example.invalid"}.items():
        monkeypatch.setenv(key, value)

    def client(*a, **k):
        built["firestore"] += 1
        return MagicMock()

    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", client)
    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", _Queue)
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))
    real_drive = indexer_main.OneDriveAdapter

    def drive(*args, **kwargs):
        built["drive"] = kwargs
        return real_drive(*args, **kwargs)

    monkeypatch.setattr(indexer_main, "OneDriveAdapter", drive)
    return built


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_key_refuses_the_start_before_any_client(env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("KMS_CRYPTO_KEY", value)
    with pytest.raises(ValueError, match="KMS_CRYPTO_KEY"):
        indexer_main.build_app()
    assert env["firestore"] == 0


def test_a_missing_key_refuses_even_the_first_deploy_pass(env, monkeypatch):
    monkeypatch.delenv("DRIVE_INDEXER_URL")
    with pytest.raises(ValueError, match="KMS_CRYPTO_KEY"):
        indexer_main.build_app()


def test_with_the_key_the_drive_reads_the_encrypted_store(env, monkeypatch):
    monkeypatch.setenv("KMS_CRYPTO_KEY", KEY_NAME)
    indexer_main.build_app()
    store = env["drive"]["oauth_credentials"]
    assert isinstance(store, FirestoreCredentialStore)
    assert isinstance(store._cipher, GcpKmsCipherAdapter) and store._cipher._key_name == KEY_NAME
