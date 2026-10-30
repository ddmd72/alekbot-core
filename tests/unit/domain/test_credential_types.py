"""The credential domain types of multi-drive slice 1a (MULTI_DRIVE_RFC §3, §4, §5): field sets, frozen value
objects, defaults, and the provider-edge port's shape."""
import dataclasses
import inspect
from datetime import datetime, timezone

import pytest

from src.domain.connector_spec import ConnectorSpec
from src.domain.credential_key import CredentialKey
from src.domain.flow_intent import FlowIntent
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.oauth_flow import OAuthFlow
from src.domain.oauth_grant import GrantResult
from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.oauth_provider_port import OAuthProviderPort

WHEN = datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)
ACCOUNT = ProviderAccount(realm="microsoft", subject="sub-1", label="alex@example.com")


def _fields(cls) -> list:
    return [f.name for f in dataclasses.fields(cls)]


def test_enums():
    assert [i.value for i in Integration] == ["mail", "tasks", "drive"]
    assert [i.name for i in FlowIntent] == ["CONNECT", "ADD", "RECONNECT"]
    assert [i.name for i in RevocationScope] == ["GRANT_WIDE", "NONE"]
    assert Integration("drive") is Integration.DRIVE


def test_oauth_credential_fields_and_defaults():
    assert _fields(OAuthCredential) == [
        "credential_id", "user_id", "account_id", "kind", "integration", "account", "access_token", "refresh_token",
        "token_expiry", "scopes", "needs_reconnect", "drive_id"]
    cred = OAuthCredential(user_id="user-alex", account_id="acct-1", kind="microsoft_onedrive",
                           integration=Integration.DRIVE, account=ACCOUNT, access_token="at", refresh_token="rt",
                           token_expiry=WHEN, scopes=[])
    assert (cred.credential_id, cred.needs_reconnect, cred.drive_id) == ("", False, "")


def test_oauth_credential_is_keyword_only_and_needs_its_identity():
    with pytest.raises(TypeError):
        OAuthCredential("user-alex")   # type: ignore[misc]
    with pytest.raises(TypeError):
        OAuthCredential(user_id="user-alex", kind="gmail", account=ACCOUNT, access_token="at",   # type: ignore[call-arg]
                        refresh_token="rt", token_expiry=WHEN, scopes=[])


@pytest.mark.parametrize("value", [
    ACCOUNT,
    CredentialKey(user_id="user-alex", credential_id="cred-1"),
    GrantResult(access_token="at", refresh_token="rt", token_expiry=WHEN, scopes=("s",), account=ACCOUNT),
    ConnectorSpec(kind="k", integration=Integration.MAIL, realm="google", required_scopes=("s",), multiple=False,
                  id_space="", label="L"),
    OAuthFlow(user_id="user-alex", kind="k", intent=FlowIntent.CONNECT, target_id="", expected_subject="",
              hint_label="", pkce_verifier="v", return_path="/cabinet", expires_at=WHEN),
])
def test_value_objects_are_frozen(value):
    field = _fields(type(value))[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, "x")


def test_value_object_fields():
    assert _fields(ProviderAccount) == ["realm", "subject", "label"]
    assert _fields(CredentialKey) == ["user_id", "credential_id"]
    assert _fields(GrantResult) == ["access_token", "refresh_token", "token_expiry", "scopes", "account"]
    assert _fields(ConnectorSpec) == ["kind", "integration", "realm", "required_scopes", "multiple", "id_space",
                                      "label"]
    assert _fields(OAuthFlow) == ["user_id", "kind", "intent", "target_id", "expected_subject", "hint_label",
                                  "pkce_verifier", "return_path", "expires_at"]


def test_the_credential_port_is_rekeyed_by_credential():
    assert set(OAuthCredentialsPort.__abstractmethods__) == {
        "get", "save", "update", "delete", "list_for_user", "list_keys", "list_by_account", "list_user_ids"}
    params = {name: list(inspect.signature(getattr(OAuthCredentialsPort, name)).parameters)[1:]
              for name in OAuthCredentialsPort.__abstractmethods__}
    assert params == {"get": ["user_id", "credential_id"], "save": ["credential"], "update": ["credential"],
                      "delete": ["user_id", "credential_id"], "list_for_user": ["user_id", "kind"],
                      "list_keys": ["user_id", "kind"],
                      "list_by_account": ["realm", "subject"], "list_user_ids": ["kind"]}
    assert inspect.signature(OAuthCredentialsPort.list_for_user).parameters["kind"].default is None
    assert inspect.signature(OAuthCredentialsPort.list_keys).parameters["kind"].default is None
    assert inspect.signature(OAuthCredentialsPort.save).return_annotation is str


def test_the_provider_port_shape():
    assert set(OAuthProviderPort.__abstractmethods__) == {
        "realm", "revocation_scope", "authorization_url", "exchange", "revoke"}
    url = inspect.signature(OAuthProviderPort.authorization_url).parameters
    assert list(url)[1:] == ["flow", "spec", "redirect_uri", "state"]
    assert url["state"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(OAuthProviderPort.exchange).parameters)[1:] == [
        "code", "flow", "spec", "redirect_uri"]
    assert inspect.iscoroutinefunction(OAuthProviderPort.exchange)
    assert inspect.iscoroutinefunction(OAuthProviderPort.revoke)
