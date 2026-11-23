"""A revoked device session is refused by every route that asks who is signed in (CABINET_DEVICE_SESSIONS_RFC §4 "The
gate is not the only reader of the cookie", §7.1 "The gate").

One contract, parametrized by subject: the gate of the Cabinet and of the call page (`make_auth_required`) and the eight
routes that used to read the access cookie themselves. Each is built as main.py builds it, with the real verifier of
`CabinetSessionKit`; everything behind the route is a `Tripwire`. With the session live, the route gets past its
session check (it touches what lies behind, or answers as signed in); once that session is revoked, the same request
is refused and touches nothing. `test_access_cookie_one_reader.py` keeps a ninth reader from appearing.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable, List

import pytest
from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.services.file_access_token_service import FileAccessTokenService
from src.web import user_cabinet_app
from src.web.file_access_app import create_file_access_blueprint
from src.web.landing_app import create_landing_blueprint
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from src.web.oauth_app import create_oauth_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.cabinet_session_kit import ALEX_ID, CabinetSessionKit, SignedIn
from tests.unit.web.gate_fakes import Tripwire

FILE_SECRET = "file-link-secret-0123456789abcdef-xyz"


class World:
    def __init__(self) -> None:
        self.kit = CabinetSessionKit()
        self.touched: List[str] = []

    def wire(self, name: str) -> Tripwire:
        return Tripwire(name, self.touched)


def _cabinet(w: World):
    wire = w.wire
    return create_user_cabinet_blueprint(
        invite_service=wire("invite_service"), cabinet_sessions=w.kit.verifier, user_repo=wire("user_repo"),
        fact_repo=wire("fact_repo"), embedding_service=wire("embedding_service"),
        oauth_credentials_port=wire("oauth_credentials_port"), credential_revocation=wire("credential_revocation"),
        indexed_email_repo=wire("indexed_email_repo"), email_indexing_service=wire("email_indexing_service"),
        email_job_repo=wire("email_job_repo"), task_queue=wire("task_queue"), task_setup=wire("task_setup"),
        tasks_provider=wire("tasks_provider"), language_service=wire("language_service"),
        agent_note_port=wire("agent_note_port"), recurrence_port=wire("recurrence_port"),
        twilio_verify_client=wire("twilio_verify_client"), user_drive_service=wire("user_drive_service"),
        drive_search_service=wire("drive_search_service"), drive_task_queue=wire("drive_task_queue"),
        drive_transcribe_switch_on=True, cabinet_facts=wire("cabinet_facts"),
        fact_review_store=wire("fact_review_store"), fact_answer_service=wire("fact_answer_service"),
        localization=wire("localization"), connectors=ConnectorRegistry())


def _oauth(w: World):
    return create_oauth_blueprint(
        auth_service=w.wire("auth_service"), cabinet_sessions=w.kit.verifier,
        device_sessions=w.kit.device_sessions, auth_registry=w.wire("auth_registry"),
        auth_config=type("Cfg", (), {"oauth_redirect_uri": "https://cabinet.example.com/auth/callback"})(),
        invite_service=w.wire("invite_service"), connect_service=w.wire("connect_service"),
        connectors=ConnectorRegistry())


def _voice_web_call(w: World):
    return create_voice_web_call_blueprint(
        cabinet_sessions=w.kit.verifier, call_setup=w.wire("call_setup"), media_room=w.wire("media_room"),
        ephemeral_store=w.wire("ephemeral_store"), relay_base_url="wss://relay.example", cipher=AesGcmTestCipher(),
        digest_key=b"k" * 32, language_service=w.wire("language_service"))


def _file_access(w: World):
    return create_file_access_blueprint(token_service=FileAccessTokenService(FILE_SECRET),
                                        media_storage=w.wire("media_storage"), cabinet_sessions=w.kit.verifier)


def _landing(w: World):
    return create_landing_blueprint(w.kit.verifier)


def _mcp_consent(w: World):
    return build_mcp_consent_blueprint(mcp_service=w.wire("mcp_service"), cabinet_sessions=w.kit.verifier)


Send = Callable[..., Awaitable]


@dataclass(frozen=True)
class Subject:
    """A route that asks who is signed in: how to build its blueprint, how to call it as `signed_in`, and what shows
    that the call got past the session check (`passed(response, touched)`)."""
    build: Callable[[World], object]
    send: Callable[[object, SignedIn], Awaitable]
    passed: Callable[[object, List[str]], bool]


def _cookie(client, s: SignedIn) -> object:
    CabinetSessionKit.set_cookies(client, s, refresh=False)
    return client


def _is_login_redirect(resp) -> bool:
    return resp.status_code == 302 and resp.headers["Location"].startswith("/auth/login")


SUBJECTS = {
    "the Cabinet gate (make_auth_required)": Subject(
        _cabinet, lambda c, s: c.delete("/api/user/link-platform?platform=slack", headers=s.bearer),
        lambda r, t: "user_repo.remove_platform_id" in t),
    "the call page gate (make_auth_required)": Subject(
        _voice_web_call, lambda c, s: _cookie(c, s).get("/api/voice/ui-language"),
        lambda r, t: "language_service.resolve_ui_language" in t),
    "landing_app /": Subject(
        _landing, lambda c, s: _cookie(c, s).get("/"),
        lambda r, t: r.status_code == 302 and r.headers["Location"] == "/cabinet"),
    "mcp_consent_app /mcp/consent": Subject(
        _mcp_consent, lambda c, s: _cookie(c, s).get("/mcp/consent?req=consent-request"),
        lambda r, t: "mcp_service.verify_consent_request" in t),
    "file_access_app /f/<token> (gated)": Subject(
        _file_access,
        lambda c, s: _cookie(c, s).get(
            f"/f/{FileAccessTokenService(FILE_SECRET).mint('reports/review.html', ALEX_ID, gated=True)}"),
        lambda r, t: "media_storage.exists" in t),
    "oauth_app _session (/auth/connect/<kind>)": Subject(
        _oauth, lambda c, s: _cookie(c, s).get("/auth/connect/no-such-kind"),
        lambda r, t: r.status_code == 404),
    "oauth_app /auth/me": Subject(
        _oauth, lambda c, s: _cookie(c, s).get("/auth/me"), lambda r, t: r.status_code == 200),
    "oauth_app /auth/link-oauth": Subject(
        _oauth,
        lambda c, s: _link(c, s),
        lambda r, t: "auth_service.link_oauth_identity" in t),
    "user_cabinet_app owner_only (/cabinet/docs)": Subject(
        _cabinet, lambda c, s: _cookie(c, s).get("/cabinet/docs/index.html"), lambda r, t: r.status_code == 200),
    "user_cabinet_app /join": Subject(
        _cabinet, lambda c, s: _cookie(c, s).get("/join?code=TEAM-1"),
        lambda r, t: "invite_service.consume_team_invite" in t),
}


def _link(client, s: SignedIn):
    _cookie(client, s).set_cookie("localhost", "oauth_state", "st-1")
    return client.post("/auth/link-oauth", json={"code": "c1", "state": "st-1"})


@pytest.fixture(autouse=True)
def _docs_owner(monkeypatch):
    monkeypatch.setattr(user_cabinet_app, "DOCS_OWNER_USER_ID", ALEX_ID)

    async def docs_page(directory, filename):
        return "the docs page"
    monkeypatch.setattr(user_cabinet_app, "send_from_directory", docs_page)


async def _call(subject: Subject, revoke: bool):
    world = World()
    app = Quart("revoked_session")
    app.register_blueprint(subject.build(world))
    signed = await world.kit.sign_in()
    if revoke:
        await world.kit.revoke(signed)
    resp = await subject.send(app.test_client(), signed)
    return resp, world.touched


@pytest.mark.parametrize("name", SUBJECTS)
async def test_a_live_session_gets_past_the_session_check(name):
    subject = SUBJECTS[name]
    resp, touched = await _call(subject, revoke=False)
    assert subject.passed(resp, touched), (resp.status_code, touched)


@pytest.mark.parametrize("name", SUBJECTS)
async def test_the_same_request_from_a_revoked_session_is_refused_and_touches_nothing(name):
    subject = SUBJECTS[name]
    resp, touched = await _call(subject, revoke=True)
    assert not subject.passed(resp, touched)
    assert touched == []
    # refused: a 401/404, a redirect to the sign-in, or (the landing page) the public page
    assert resp.status_code in (200, 302, 401, 404) and (resp.status_code != 302 or _is_login_redirect(resp))
