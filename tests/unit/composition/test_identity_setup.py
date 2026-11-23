"""`build_identity`: one account, user and whitelist repository shared by IAM, invites and the Cabinet's sign-in, the
sign-in over the AuthConfig it is given, and the phone-binding client only with all three Twilio settings."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition import identity_setup
from src.composition.identity_setup import build_identity
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

    identity = build_identity({}, db, env, auth_config=AUTH, digest_key=b"digest-1")  # type: ignore[arg-type]

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
    device_sessions = identity_setup.build_device_sessions(db, env, session_service=session, notification_service=notices)  # type: ignore[arg-type]
    assert device_sessions._sessions is session and device_sessions._notifications is notices


def test_with_all_three_twilio_settings_the_verify_service_is_built(env):
    identity = build_identity(TWILIO, EvalDb(), env, auth_config=AUTH, digest_key=b"d")  # type: ignore[arg-type]

    kind, client, service_sid = identity.twilio_verify_client
    assert kind == "verify" and client.credentials == ("AC-test-1", "token-1") and service_sid == "VA-test-1"


@pytest.mark.parametrize("missing", sorted(TWILIO))
def test_without_any_one_twilio_setting_there_is_no_verify_client(env, missing):
    config = {k: v for k, v in TWILIO.items() if k != missing}

    identity = build_identity(config, EvalDb(), env, auth_config=AUTH, digest_key=b"d")  # type: ignore[arg-type]

    assert identity.twilio_verify_client is None


def test_the_bundle_is_frozen(env):
    identity = build_identity({}, EvalDb(), env, auth_config=AUTH, digest_key=b"d")  # type: ignore[arg-type]

    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.user_repo = None  # type: ignore[misc]
