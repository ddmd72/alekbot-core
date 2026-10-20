"""T6: sandbox caller gate (RFC §4.4). Mocked at google.oauth2.id_token.verify_oauth2_token."""
import dataclasses
from typing import runtime_checkable
import logging
from unittest.mock import patch

import pytest
from quart import Quart

from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxOp
from src.web import sandbox_app
from src.web.sandbox_caller_gate import GateDecision, SandboxCallerGate

URL = "https://sandbox.example.com"
TARGET = "src.web.worker_oidc_verifier.google_id_token.verify_oauth2_token"
RENDER_MAIL = "render-caller@example.com"
INDEXER_MAIL = "indexer-caller@example.com"
SETS = {"RENDER": frozenset({RENDER_MAIL}), "INDEXER": frozenset({INDEXER_MAIL})}
PREFIXES = {"RENDER": frozenset({"render"}), "INDEXER": frozenset({"drive", "media"})}


def claims(email, aud=URL, verified=True):
    return {"email": email, "email_verified": verified, "aud": aud}


@pytest.fixture
def cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "sandbox")


def make_gate(sets=SETS, url=URL):
    return SandboxCallerGate(sets, PREFIXES, url)


def test_allowed_email_for_its_family(cloud_run):
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        d = make_gate().authorize("Bearer t", "render.docx")
    assert d.allowed is True and d.reason is None


def test_email_allowed_for_render_is_refused_for_drive_op(cloud_run):
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        d = make_gate().authorize("Bearer t", "drive.parse")
    assert d.allowed is False and d.reason == "forbidden_op"


def test_group_with_several_prefixes(cloud_run):
    with patch(TARGET, return_value=claims(INDEXER_MAIL)):
        gate = make_gate()
        assert gate.authorize("Bearer t", "drive.parse").allowed
        assert gate.authorize("Bearer t", "media.audio").allowed
        assert gate.authorize("Bearer t", "render.docx").reason == "forbidden_op"


def test_email_not_in_any_set_refused(cloud_run):
    with patch(TARGET, return_value=claims("stranger@example.com")):
        d = make_gate().authorize("Bearer t", "render.docx")
    assert d.allowed is False and d.reason == "forbidden_op"


def test_unauthenticated_when_token_invalid(cloud_run):
    with patch(TARGET, side_effect=ValueError("bad")):
        d = make_gate().authorize("Bearer t", "render.docx")
    assert d.allowed is False and d.reason == "unauthenticated"
    assert make_gate().authorize(None, "render.docx").reason == "unauthenticated"


def test_odd_op_values_are_refused_never_raise(cloud_run):
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        gate = make_gate()
        for op in ("", ".", "..docx", "render", "RENDER.docx", "x" * 10000, "\x00", "render/../x"):
            d = gate.authorize("Bearer t", op)
            assert d.allowed is False and d.reason == "forbidden_op"


def test_audience_pinned_to_service_url(cloud_run):
    with patch(TARGET, return_value=claims(RENDER_MAIL, aud="https://other.example.com")):
        d = make_gate().authorize("Bearer t", "render.docx")
    assert d.allowed is False and d.reason == "unauthenticated"


def test_x_serverless_authorization_is_ignored(cloud_run):
    # The gate's only input is the Authorization header value; the app passes nothing else.
    with patch(TARGET, return_value=claims(RENDER_MAIL)) as m:
        d = make_gate().authorize(None, "render.docx")
    assert d.allowed is False
    m.assert_not_called()


def test_cloud_run_with_empty_map_refuses_every_call(cloud_run):
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        assert make_gate(sets={}).authorize("Bearer t", "render.docx").allowed is False
        assert make_gate(url="").authorize("Bearer t", "render.docx").allowed is False


def test_local_without_k_service_is_open(monkeypatch, caplog):
    monkeypatch.delenv("K_SERVICE", raising=False)
    caplog.set_level(logging.INFO)
    gate = make_gate(sets={}, url="")
    assert "open" in caplog.text.lower()
    d = gate.authorize(None, "render.docx")
    assert d.allowed is True and d.reason is None


def test_no_email_in_log_lines(cloud_run, caplog):
    caplog.set_level(logging.DEBUG)
    with patch(TARGET, return_value=claims("stranger@example.com")):
        make_gate().authorize("Bearer t", "render.docx")
    assert "Sandbox gate" in caplog.text
    assert "stranger" not in caplog.text


def test_gate_decision_satisfies_the_app_protocol():
    proto = runtime_checkable(sandbox_app.GateDecision)
    d = GateDecision(allowed=False, reason="forbidden_op")
    assert isinstance(d, proto)
    assert (d.allowed, d.reason) == (False, "forbidden_op")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.allowed = True  # type: ignore[misc]


def test_audience_with_trailing_slash_or_prefix_refused(cloud_run):
    for aud in (URL + "/", URL + "/x", "x" + URL):
        with patch(TARGET, return_value=claims(RENDER_MAIL, aud=aud)):
            assert make_gate().authorize("Bearer t", "render.docx").reason == "unauthenticated"


def test_local_partial_config_refuses(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        assert make_gate(url="").authorize("Bearer t", "render.docx").allowed is False
        assert make_gate(sets={}).authorize("Bearer t", "render.docx").allowed is False


def test_local_full_config_enforces(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    with patch(TARGET, return_value=claims(RENDER_MAIL)):
        assert make_gate().authorize("Bearer t", "render.docx").allowed is True
        assert make_gate().authorize("Bearer t", "drive.x").allowed is False
    assert make_gate().authorize(None, "render.docx").allowed is False


def test_group_without_prefixes_fails_loudly():
    with pytest.raises(ValueError) as exc:
        SandboxCallerGate({"RENDR": frozenset({RENDER_MAIL})}, PREFIXES, URL)
    assert "RENDR" in str(exc.value) and "example.com" not in str(exc.value)


async def test_blueprint_accepts_the_gate(tmp_path, cloud_run):
    from tests.unit.web.test_sandbox_app import CEILING, FakeEngine, FakeRecycler

    engine = FakeEngine(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    app = Quart(__name__)
    app.register_blueprint(
        sandbox_app.create_sandbox_blueprint(
            {SandboxOp.RENDER_DOCX: engine}, make_gate(), FakeRecycler(), CEILING,
            {"work_root": str(work), "engine_user": None, "recycle": "always", "output_root": str(engine.out_root)},
        )
    )
    resp = await app.test_client().post("/v1/render.docx", data=b"x")
    assert resp.status_code == 401
