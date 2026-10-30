"""The generic connect routes, kind by kind (MULTI_DRIVE_RFC §4.2, §5.2, slice 1a), over the real stack of
`connect_world.py`. Ported from the deleted per-integration callback tests (`test_consent_identity.py`,
`test_consent_scope_check.py`, `test_connect_callbacks_session_account.py`, `test_oauth_app_onedrive.py`), whose
routes and `GoogleOAuthService` are gone:

- identity at every consent edge (SECRETS_AT_REST_RFC §2.3, I9): each kind stores the account subject and label the
  edge read, and a failed or incomplete identity read stores nothing, keeps an existing record and starts no To Do
  setup;
- the service scope: a grant without the kind's own scope stores nothing, whatever else it carries (the check moved
  from `GoogleOAuthService.exchange_code` to `OAuthConnectService` after the exchange, §5.2, so the identity read is
  no longer skipped for such a grant);
- the stored credential carries the spec's kind and integration and the signed-in session's account; without that
  session, or with one that has no account, nothing is exchanged or stored;
- every kind is single-credential in 1a (`multiple` False): another account's consent replaces the user's record of
  that kind and leaves the user's other kinds and other users alone; the same account's keeps its credential_id.
Examples from the invented cast only."""
import pytest

from src.domain.integration import Integration
from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import ALEX, CLARA, GOOGLE_TOKEN, GRANTED, GRAPH_ME, MS_TOKEN, USERINFO, ConnectWorld

KINDS = ("gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive")
GOOGLE_KINDS = ("gmail", "google_tasks")
MICROSOFT_KINDS = ("microsoft_todo", "microsoft_onedrive")
REALM = {"gmail": "google", "google_tasks": "google", "microsoft_todo": "microsoft", "microsoft_onedrive": "microsoft"}
INTEGRATION = {"gmail": Integration.MAIL, "google_tasks": Integration.TASKS, "microsoft_todo": Integration.TASKS,
               "microsoft_onedrive": Integration.DRIVE}
TOKEN = {"google": GOOGLE_TOKEN, "microsoft": MS_TOKEN}
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
REVOKE = "https://oauth2.googleapis.com/revoke"


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        w = ConnectWorld(monkeypatch)
        w.http.route("POST", REVOKE, body={})
        yield w


def _connected(kind: str) -> str:
    return f"/cabinet?connected={kind}"


# ------------------------------------------------------------------------------------------- identity (I9)

@pytest.mark.parametrize("kind", KINDS)
async def test_each_consent_stores_the_account_subject_and_label(world, kind):
    resp = await world.connect(kind)
    assert resp.headers["Location"] == _connected(kind)
    (got,) = await world.held(kind)
    assert (got.account.realm, got.account.subject, got.account.label) == (REALM[kind], "sub-1", "alex@example.com")
    (doc,) = world.db.collection("eval_credentials").docs.values()
    assert doc["account"] == {"realm": REALM[kind], "subject": "sub-1"}
    assert world.http.unrouted == []


async def test_a_microsoft_account_without_mail_takes_the_principal_name(world):
    world.http.route("GET", GRAPH_ME, body={"id": "sub-1", "mail": None, "userPrincipalName": "user@example.com"})
    await world.connect("microsoft_todo")
    (got,) = await world.held("microsoft_todo")
    assert got.account.label == "user@example.com"


@pytest.mark.parametrize("kind", GOOGLE_KINDS)
@pytest.mark.parametrize("status,body", [
    (401, {"error": "invalid_token"}),
    (200, {"email": "alex@example.com"}),
    (200, {"sub": "sub-1"}),
    (200, {"sub": "", "email": "alex@example.com"}),
])
async def test_a_failed_google_identity_read_stores_nothing(world, kind, status, body):
    world.http.route("GET", USERINFO, status=status, body=body)
    resp = await world.connect(kind)
    assert resp.headers["Location"] == f"/cabinet?connect_error=exchange&kind={kind}"
    assert world.db.collection("eval_credentials").docs == {}


@pytest.mark.parametrize("kind", GOOGLE_KINDS)
async def test_an_unreachable_userinfo_stores_nothing(world, kind):
    world.http._routes = [r for r in world.http._routes if r.prefix != USERINFO]
    resp = await world.connect(kind)
    assert resp.headers["Location"] == f"/cabinet?connect_error=exchange&kind={kind}"
    assert world.db.collection("eval_credentials").docs == {}


@pytest.mark.parametrize("kind", MICROSOFT_KINDS)
@pytest.mark.parametrize("status,body", [
    (403, {"error": {"code": "Authorization_RequestDenied"}}),
    (200, {"mail": "alex@example.com"}),
    (200, {"id": "", "mail": "alex@example.com"}),
])
async def test_a_failed_microsoft_identity_read_stores_nothing(world, kind, status, body):
    world.http.route("GET", GRAPH_ME, status=status, body=body)
    resp = await world.connect(kind)
    assert resp.headers["Location"] == f"/cabinet?connect_error=exchange&kind={kind}"
    assert world.db.collection("eval_credentials").docs == {}


async def test_a_failed_identity_read_keeps_an_existing_record(world):
    await world.connect("microsoft_todo")
    before = {k: dict(v) for k, v in world.db.collection("eval_credentials").docs.items()}
    world.http.route("GET", GRAPH_ME, status=401, body={})
    await world.connect("microsoft_todo")
    assert world.db.collection("eval_credentials").docs == before


async def test_a_failed_identity_read_does_not_enqueue_the_to_do_setup(world):
    world.http.route("GET", GRAPH_ME, status=401, body={})
    await world.connect("microsoft_todo")
    assert world.worker_tasks() == [] and world.db.collection("eval_credentials").docs == {}


# ------------------------------------------------------------------------------------------- the service scope

@pytest.mark.parametrize("kind", GOOGLE_KINDS)
async def test_the_granted_scopes_are_stored(world, kind):
    await world.connect(kind)
    (got,) = await world.held(kind)
    assert sorted(got.scopes) == sorted(GRANTED[kind].split())


@pytest.mark.parametrize("kind, granted", [
    ("gmail", "openid email"),
    ("gmail", "email openid https://www.googleapis.com/auth/other"),
    ("google_tasks", "openid email"),
    ("google_tasks", f"openid email {GMAIL_SCOPE}"),          # the other service's scope does not count
    ("gmail", f"openid email {TASKS_SCOPE}"),
    ("microsoft_todo", "offline_access User.Read"),
    ("microsoft_onedrive", "Tasks.ReadWrite offline_access User.Read"),
])
async def test_a_grant_without_the_kinds_own_scope_stores_nothing(world, kind, granted):
    world.granted = granted
    resp = await world.connect(kind)
    assert resp.headers["Location"] == f"/cabinet?connect_error=scope&kind={kind}"
    assert world.db.collection("eval_credentials").docs == {} and world.worker_tasks() == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_token_answer_without_a_scope_field_stores_nothing(world, kind):
    world.http.route("POST", TOKEN[REALM[kind]], body={"access_token": "at-1", "refresh_token": "rt-1",
                                                       "expires_in": 3600})
    resp = await world.connect(kind)
    assert resp.headers["Location"] == f"/cabinet?connect_error=scope&kind={kind}"
    assert world.db.collection("eval_credentials").docs == {}


# ------------------------------------------------------------------------------------------- spec and session

@pytest.mark.parametrize("kind", KINDS)
async def test_the_credential_carries_the_spec_and_the_sessions_account(world, kind):
    resp = await world.connect(kind)
    assert resp.headers["Location"] == _connected(kind)
    (cred,) = await world.store.list_for_user(ALEX)
    assert (cred.kind, cred.integration, cred.account_id, cred.user_id) == (kind, INTEGRATION[kind], "acct-1", ALEX)
    assert cred.refresh_token == f"rt-{kind}"
    (doc,) = world.db.collection("eval_credentials").docs.values()
    assert (doc["kind"], doc["integration"], doc["account_id"]) == (kind, INTEGRATION[kind].value, "acct-1")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("token", [None, "not-a-jwt"], ids=["no-session", "invalid-session"])
async def test_no_signed_in_session_stores_nothing_and_exchanges_nothing(world, kind, token):
    _, state = await world.start(kind)
    resp = await world.callback(state, token=token)
    assert resp.headers["Location"] == "/auth/login?next=/cabinet"
    assert world.db.collection("eval_credentials").docs == {}
    assert world.http.requests_to("POST", TOKEN[REALM[kind]]) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_session_without_an_account_stores_nothing(world, kind):
    """A credential is never saved under a guessed tenant: a session whose payload names no account is sent to the
    sign-in before any exchange."""
    _, state = await world.start(kind)
    resp = await world.callback(state, token=world.session(ALEX, account_id=""))
    assert resp.headers["Location"] == "/auth/login?next=/cabinet"
    assert world.db.collection("eval_credentials").docs == {}
    assert world.http.requests_to("POST", TOKEN[REALM[kind]]) == []


# ------------------------------------------------------------------------------------------- one per kind (1a)

@pytest.mark.parametrize("kind", KINDS)
async def test_another_accounts_consent_replaces_the_record_of_that_kind(world, kind):
    other = "microsoft_todo" if kind != "microsoft_todo" else "gmail"
    await world.connect(other)                                                  # another kind of the user
    await world.connect(kind, token=world.session(CLARA, "acct-2"))             # another user, same kind
    await world.connect(kind)
    (first,) = await world.held(kind)
    world.signs_in_as("sub-2")
    resp = await world.connect(kind)
    assert resp.headers["Location"] == _connected(kind)
    (now,) = await world.held(kind)
    assert now.account.subject == "sub-2" and now.credential_id != first.credential_id
    assert len(await world.held(other)) == 1
    assert len(await world.held(kind, CLARA)) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_the_same_accounts_consent_keeps_the_credential_id(world, kind):
    await world.connect(kind)
    (first,) = await world.store.list_for_user(ALEX)
    await world.connect(kind)
    (again,) = await world.store.list_for_user(ALEX)
    assert again.credential_id == first.credential_id
