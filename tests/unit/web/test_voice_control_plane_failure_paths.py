"""Failure paths of the voice control plane after its OIDC gate (P7, C1: `src/web/voice_control_plane_app.py` held
at 100 % lines): a late answer with no chat sink, an unreadable AMD verdict, and an alert sink that is down.

The real Firestore ephemeral store on the evaluating fake holds tickets, verdicts and late answers; the billing,
prompt-content and summary neighbours record what they were given."""
from quart import Quart

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.firestore_eval import EvalDb, eval_transactions

RELAY = {"Authorization": "Bearer relay-token"}
CALL = {"call_id": "c1", "user_id": "u1", "account_id": "a1", "transcript_text": "caller: book a table",
        "usage_by_model": {}, "turns": []}


async def _relay_only(authorization):     # the gate itself: test_voice_submit_transcript_requires_oidc.py
    return authorization == RELAY["Authorization"]


class Recorder:
    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list = []

    async def post(self, text: str) -> None:
        self.calls.append(text)
        if self.fails:
            raise RuntimeError("alert webhook 500")

    async def summary(self, **kwargs) -> None:
        self.calls.append(kwargs["call_id"])
        if self.fails:
            raise RuntimeError("lelik_summarizer prompt missing")

    async def flush(self) -> None:
        pass

    async def record_turn(self, **kwargs) -> None:
        self.calls.append(kwargs)


class World:
    def __init__(self, summary_fails: bool = False, alert_fails: bool = False) -> None:
        self.db = EvalDb()
        self.store = FirestoreEphemeralStore(self.db, collection="voice")
        self.summaries = Recorder(fails=summary_fails)
        self.alerts = Recorder(fails=alert_fails)
        app = Quart("voice_control_plane_c1")
        app.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=Recorder(), prompt_content_store=Recorder(),
            summary_consumer=self.summaries.summary, oidc_verifier=_relay_only, alert_sink=self.alerts, digest_key=b"k" * 32
        ))                                   # no late_answer_sink: this deployment cannot post late answers
        self.client = app.test_client()

    def keys(self) -> set:
        return set(self.db.collection("voice").docs)


async def test_an_abandoned_answer_with_no_chat_sink_is_consumed_and_dropped():
    w = World()
    with eval_transactions():
        await w.store.set("voice_delegation_result:t1:c9", {"output": "the answer", "request": "q",
                                                            "user_id": "u1", "account_id": "a1"}, ttl_s=600)
        resp = await w.client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c9"}, headers=RELAY)
    assert resp.status_code == 200
    assert "voice_delegation_result:t1:c9" not in w.keys()     # claimed once, so it can never be posted twice
    assert w.alerts.calls == []


async def test_an_unreadable_amd_verdict_counts_as_a_live_call_and_the_summary_is_written():
    w = World()
    await w.store.set("voice_one_call:u1", {"in_flight": True}, ttl_s=600)
    await w.db.collection("voice").document("voice_amd:c1").set({"value": {"answered_by": "machine_start"}})  # no expiry
    resp = await w.client.post("/voice/submit-transcript", json=CALL, headers=RELAY)
    assert resp.status_code == 200
    assert w.summaries.calls == ["c1"]
    assert "voice_one_call:u1" not in w.keys()


async def test_a_summary_failure_whose_alert_also_fails_still_closes_the_call():
    w = World(summary_fails=True, alert_fails=True)
    await w.store.set("voice_one_call:u1", {"in_flight": True}, ttl_s=600)
    resp = await w.client.post("/voice/submit-transcript", json=CALL, headers=RELAY)
    assert resp.status_code == 200
    assert len(w.alerts.calls) == 1 and "summary failed for call c1" in w.alerts.calls[0]
    assert "voice_one_call:u1" not in w.keys()                 # the marker is released all the same
