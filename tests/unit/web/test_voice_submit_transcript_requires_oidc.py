"""/voice/submit-transcript refuses a caller without the relay's OIDC token (P7, inventory §4;
`src/web/voice_control_plane_app.py:175-179`): 401, nothing billed, no summary, no prompt-content row.

The gate is the one main.py builds (`build_oidc_gate` over a non-empty identity set, as deployed)."""
import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.worker_oidc_verifier import build_oidc_gate
from tests.unit.web.gate_fakes import Tripwire

PAYLOAD = {
    "call_id": "c1", "user_id": "u1", "account_id": "a1", "transcript_text": "caller: hi",
    "usage_by_model": {"gpt-realtime-2.1": {"audio_input_tokens": 100}},
    "turns": [{"request_text": "hi", "response_text": "hello", "finish_reason": "completed",
               "started_at": "2026-10-22T10:00:00+00:00", "ended_at": "2026-10-22T10:00:02+00:00"}],
}


def _client(touched: list):
    async def summary_consumer(**kwargs):
        touched.append("summary_consumer")

    app = Quart("submit_transcript_gate")
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=Tripwire("ephemeral_store", touched),
        quota_service=Tripwire("quota_service", touched),
        prompt_content_store=Tripwire("prompt_content_store", touched),
        summary_consumer=summary_consumer,
        oidc_verifier=build_oidc_gate(frozenset({"relay@example.test"}), on_cloud_run=True, label="P7"),
        alert_sink=Tripwire("alert_sink", touched), digest_key=b"k" * 32
    ))
    return app.test_client()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer "}])
async def test_without_the_relay_token_is_401_and_nothing_is_billed_or_summarised(headers):
    touched: list = []
    resp = await _client(touched).post("/voice/submit-transcript", json=PAYLOAD, headers=headers)
    assert resp.status_code == 401
    assert touched == []
