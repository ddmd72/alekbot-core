"""`build_identity`: one account, user and whitelist repository shared by IAM, invites and the Cabinet's sign-in, the
sign-in over the AuthConfig it is given, and the phone-binding client only with all three Twilio settings.
`build_device_auth`: one device store under the sessions and the passkeys, the passkey relying party taken from the
sign-in callback, the challenges in their own collection."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition import identity_setup
from src.composition.identity_setup import build_device_auth, build_identity, passkey_relying_party
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb

TWILIO = {"TWILIO_ACCOUNT_SID": "AC-test-1", "TWILIO_AUTH_TOKEN": "token-1", "TWILIO_VERIFY_SERVICE_SID": "VA-test-1"}
SECRET = "session-secret-1-" + "x" * 32
AUTH = SimpleNamespace(firebase_project_id="project-1", firebase_web_api_key="web-key-1", firebase_service_account=None,
                       google_oauth_client_id="client-1", google_oauth_client_secret="client-secret-1",
                       oauth_session_secret=SECRET)


class _Firebase:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Twilio:
    def __init__(self, sid, token):
        self.credentials = (sid, token)
        self.verify = SimpleNamespace(v2=SimpleNamespace(services=lambda service_sid: ("verify", self, service_sid)))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(identity_setup, "FirebaseAuthAdapter", _Firebase)
    monkeypatch.setattr(identity_setup, "TwilioClient", _Twilio)
    return EnvironmentConfig()


def test_the_repositories_are_shared_and_the_sign_in_runs_over_the_auth_config(env):
    db = EvalDb()

    identity = build_identity({}, db, env, auth_config=AUTH, digest_key=b"digest-1", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]

    users, accounts, whitelist = identity.user_repo, identity.account_repo, identity.whitelist_repo
    assert users.db is db and users.account_repo is accounts
    assert (identity.iam_service.user_repo, identity.iam_service.account_repo,
            identity.iam_service.whitelist_repo) == (users, accounts, whitelist)
    invites = identity.invite_service
    assert (invites.user_repo, invites.account_repo, invites.whitelist_repo) == (users, accounts, whitelist)
    assert invites.repo.db is db and invites.repo._digest_key == b"digest-1"
    auth = identity.auth_service
    assert auth.auth_registry is identity.auth_registry
    assert (auth.user_repo, auth.account_repo, auth.whitelist_repo) == (users, accounts, whitelist)
    firebase = identity.auth_registry.get_provider("firebase")
    assert firebase.kwargs == {"project_id": "project-1", "web_api_key": "web-key-1", "service_account_path": None,
                               "oauth_client_id": "client-1", "oauth_client_secret": "client-secret-1"}
    session = identity.session_service
    # The token lifetimes are the device-session policy's now: the session service takes only the secret, and the
    # device sessions are signed by this very service.
    assert session.secret_key == SECRET
    notices = object()
    device_auth = identity_setup.build_device_auth(db, env, session_service=session, notification_service=notices,  # type: ignore[arg-type]
                                                   oauth_redirect_uri="https://cabinet.example.com/auth/callback",
                                                       cipher=None, digest_key=b"digest-1")  # type: ignore[arg-type]
    device_sessions = device_auth.sessions
    assert device_sessions._sessions is session and device_sessions._notifications is notices


def test_with_all_three_twilio_settings_the_verify_service_is_built(env):
    identity = build_identity(TWILIO, EvalDb(), env, auth_config=AUTH, digest_key=b"d", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]

    kind, client, service_sid = identity.twilio_verify_client
    assert kind == "verify" and client.credentials == ("AC-test-1", "token-1") and service_sid == "VA-test-1"


@pytest.mark.parametrize("missing", sorted(TWILIO))
def test_without_any_one_twilio_setting_there_is_no_verify_client(env, missing):
    config = {k: v for k, v in TWILIO.items() if k != missing}

    identity = build_identity(config, EvalDb(), env, auth_config=AUTH, digest_key=b"d", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]

    assert identity.twilio_verify_client is None


def test_the_bundle_is_frozen(env):
    identity = build_identity({}, EvalDb(), env, auth_config=AUTH, digest_key=b"d", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]

    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.user_repo = None  # type: ignore[misc]


def test_the_device_sessions_and_passkeys_share_one_store_and_the_rp_is_the_callback_host(env):
    db = EvalDb()
    identity = build_identity({}, db, env, auth_config=AUTH, digest_key=b"d", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]

    auth = build_device_auth(db, env, session_service=identity.session_service, notification_service="notices",  # type: ignore[arg-type]
                             oauth_redirect_uri="https://cabinet.example.com/auth/callback", cipher=None,  # type: ignore[arg-type]
                             digest_key=b"d")

    sessions, passkeys = auth.sessions, auth.passkeys
    assert passkeys._repo is sessions._repo
    assert (sessions._repo._col.name, sessions._repo._passkeys.name) == (env.device_sessions_collection,
                                                                       env.passkeys_collection)
    assert (sessions._sessions, sessions._notifications, passkeys._notifications) == (
        identity.session_service, "notices", "notices")
    assert (passkeys._verifier._rp_id, passkeys._verifier._origin, passkeys._verifier._rp_name) == (
        "cabinet.example.com", "https://cabinet.example.com", "Alek")
    assert (passkeys._challenges._db, passkeys._challenges._collection) == (db, env.passkey_challenges_collection)
    assert env.passkeys_collection.endswith("auth_passkeys_v1")
    assert env.passkey_challenges_collection.endswith("auth_passkey_challenges")


@pytest.mark.parametrize("uri, rp", [
    ("https://cabinet.example.com/auth/callback", ("cabinet.example.com", "https://cabinet.example.com")),
    ("https://Cabinet.Example.com:8443/auth/callback", ("cabinet.example.com", "https://cabinet.example.com:8443")),
    ("http://localhost:8080/auth/callback", ("localhost", "http://localhost:8080")),
])
def test_the_relying_party_is_the_callbacks_host_and_origin(uri, rp):
    assert passkey_relying_party(uri) == rp


@pytest.mark.parametrize("uri", ["", "/auth/callback", "cabinet.example.com/auth/callback"])
def test_a_callback_without_scheme_or_host_refuses_the_start(uri):
    with pytest.raises(ValueError, match="no scheme or host"):
        passkey_relying_party(uri)


def test_the_kill_switch_reaches_the_pin_reset_and_the_mcp_grants_and_the_pin_can_lock(env):
    db = EvalDb()
    identity = build_identity({}, db, env, auth_config=AUTH, digest_key=b"d", cabinet_url="https://main.example.test/cabinet")  # type: ignore[arg-type]
    cipher = object()

    auth = build_device_auth(db, env, session_service=identity.session_service, notification_service="notices",  # type: ignore[arg-type]
                             oauth_redirect_uri="https://cabinet.example.com/auth/callback", cipher=cipher,  # type: ignore[arg-type]
                             digest_key=b"digest-1")

    sessions, pin, lock = auth.sessions, auth.facts_pin, auth.account_lock
    assert sessions._repo._facts_pins.name == env.facts_pin_collection
    grants = sessions._revoke_connector_grants.__self__
    assert (grants._refresh.name, grants._cipher, grants._digest_key) == (
        env.mcp_refresh_tokens_collection, cipher, b"digest-1")
    assert (pin._repo._col.name, pin._repo._users.name) == (env.facts_pin_collection, env.domain_users_collection)
    assert (pin._sessions, pin._lock, pin._notifications, pin._digest_key) == (sessions._repo, lock, "notices",
                                                                              b"digest-1")
    assert (lock._device_sessions, lock._notifications) == (sessions, "notices")
    assert env.facts_pin_collection.endswith("facts_pin_v1")
