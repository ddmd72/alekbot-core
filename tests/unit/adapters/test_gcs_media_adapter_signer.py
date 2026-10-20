"""IAM least-privilege Step 0 (C2): the signed-URL signer is the runtime's own identity.

Mock boundary: google.auth.default and google.cloud.storage.Client (SDK level)."""
from unittest.mock import MagicMock, patch

import google.auth.credentials
import pytest

from src.adapters.gcs_media_adapter import GcsMediaAdapter


def _client():
    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://signed"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    return patch("google.cloud.storage.Client", return_value=client), blob


class _KeyCreds(google.auth.credentials.Signing):
    """A credential that signs locally (service-account key file)."""
    refresh = MagicMock()

    def sign_bytes(self, message):  # pragma: no cover - never called, storage client is mocked
        return b""

    @property
    def signer_email(self):
        return "k@example.invalid"

    @property
    def signer(self):
        return None


def _compute(email):
    creds = MagicMock(spec=["refresh", "token", "service_account_email"])
    creds.token = "tok"
    creds.service_account_email = email
    return creds


async def test_local_signing_credential_signs_as_before():
    ctx, blob = _client()
    with ctx, patch("google.auth.default", return_value=(_KeyCreds(), "proj")):
        await GcsMediaAdapter("b").generate_signed_url("k", 60)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert "service_account_email" not in kwargs and "access_token" not in kwargs


async def test_compute_credentials_sign_as_the_refreshed_runtime_identity():
    creds = _compute("runtime@example.invalid")
    ctx, blob = _client()
    with ctx, patch("google.auth.default", return_value=(creds, "proj")):
        await GcsMediaAdapter("b").generate_signed_url("k", 60)
    creds.refresh.assert_called_once()
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["service_account_email"] == "runtime@example.invalid"
    assert kwargs["access_token"] == "tok"


async def test_today_config_unchanged_when_override_equals_runtime_identity():
    """Today SERVICE_ACCOUNT_EMAIL == the runtime SA: signing as the credential's identity is identical."""
    creds = _compute("runtime@example.invalid")
    ctx, blob = _client()
    with ctx, patch("google.auth.default", return_value=(creds, "proj")):
        await GcsMediaAdapter("b", service_account_email="runtime@example.invalid").generate_signed_url("k", 60)
    assert blob.generate_signed_url.call_args.kwargs["service_account_email"] == "runtime@example.invalid"


async def test_explicit_override_wins():
    creds = _compute("runtime@example.invalid")
    ctx, blob = _client()
    with ctx, patch("google.auth.default", return_value=(creds, "proj")):
        await GcsMediaAdapter("b", service_account_email="other@example.invalid").generate_signed_url("k", 60)
    assert blob.generate_signed_url.call_args.kwargs["service_account_email"] == "other@example.invalid"


@pytest.mark.parametrize("email", ["default", "", None])
async def test_unresolved_signer_is_an_error(email):
    ctx, blob = _client()
    with ctx, patch("google.auth.default", return_value=(_compute(email), "proj")):
        with pytest.raises(RuntimeError):
            await GcsMediaAdapter("b").generate_signed_url("k", 60)
    blob.generate_signed_url.assert_not_called()


async def test_no_ambient_credentials_falls_through_to_the_storage_client():
    ctx, blob = _client()
    with ctx, patch("google.auth.default", side_effect=google.auth.exceptions.DefaultCredentialsError("none")):
        assert await GcsMediaAdapter("b").generate_signed_url("k", 60) == "https://signed"
    assert "service_account_email" not in blob.generate_signed_url.call_args.kwargs
