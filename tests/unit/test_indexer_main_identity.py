"""IAM least-privilege Step 0 (C1 + C3): the indexer's accepted /worker identities and the drive queue's
sending identity, from env. With only SERVICE_ACCOUNT_EMAIL set (today) nothing changes."""
from unittest.mock import MagicMock, patch

import pytest

import indexer_main
from src.composition import drive_indexing_setup

_VERIFY = "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token"


class _Queue:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def env(monkeypatch):
    sent = {}
    monkeypatch.delenv("K_SERVICE", raising=False)
    for key in ("DRIVE_INVOKER_SA_EMAIL", "OIDC_TRANSITION_SA_EMAILS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"GOOGLE_CLOUD_PROJECT": "p", "APP_ENV": "development", "GEMINI_API_KEY": "k",
                       "DRIVE_INDEXER_URL": "https://indexer.example.invalid",
                       # the cipher's key name (SECRETS_AT_REST_RFC §2.6, I6); build_cipher calls no KMS API
                       "KMS_CRYPTO_KEY": "projects/example-project/locations/global/keyRings/ring-a/cryptoKeys/key-a",
                       "MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec",
                       "SERVICE_ACCOUNT_EMAIL": "legacy@example.invalid\n"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(indexer_main.firestore, "AsyncClient", MagicMock())
    monkeypatch.setattr(indexer_main, "build_place_lookup", lambda: (lambda lat, lon: None))

    def queue(**kwargs):
        sent.update(kwargs)
        return _Queue(**kwargs)

    monkeypatch.setattr(drive_indexing_setup, "GcpTaskQueue", queue)
    return sent


async def _status(app, email):
    with patch(_VERIFY, return_value={"email": email, "email_verified": True,
                                      "aud": "https://indexer.example.invalid"}):
        resp = await app.test_client().post("/worker", json={"task_type": "nope"},
                                            headers={"Authorization": "Bearer t"})
    return resp.status_code


async def test_today_config_legacy_identity_accepted_and_sent(env):
    app = indexer_main.build_app()
    assert await _status(app, "legacy@example.invalid") == 400  # past the gate (unknown task_type)
    assert await _status(app, "other@example.invalid") == 401
    assert env["service_account_email"] == "legacy@example.invalid"


async def test_drive_invoker_replaces_legacy_for_gate_and_queue(env, monkeypatch):
    monkeypatch.setenv("DRIVE_INVOKER_SA_EMAIL", "drive@example.invalid\n")
    app = indexer_main.build_app()
    assert await _status(app, "drive@example.invalid") == 400
    assert await _status(app, "legacy@example.invalid") == 401
    assert env["service_account_email"] == "drive@example.invalid"


async def test_transition_list_widens_the_gate_only(env, monkeypatch):
    monkeypatch.setenv("DRIVE_INVOKER_SA_EMAIL", "drive@example.invalid")
    monkeypatch.setenv("OIDC_TRANSITION_SA_EMAILS", "legacy@example.invalid,-")
    app = indexer_main.build_app()
    assert await _status(app, "legacy@example.invalid") == 400
    assert env["service_account_email"] == "drive@example.invalid"
