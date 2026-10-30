"""/voice/inbound-status on a deployment where the voice companion is not configured (P7, C1:
`src/web/voice_webhook_app.py:183-185`): the callback cannot be placed, so the ticket and the one-call marker are
released at once (no lock-out until their TTL), the ops alert is posted, and Twilio still gets a 200.

A real Twilio signature (computed with Twilio's own RequestValidator) and the real Firestore ephemeral store on the
evaluating fake."""
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from quart import Quart
from twilio.request_validator import RequestValidator

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.web.twilio_signature_verifier import verify_twilio_signature
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.unit.web.gate_fakes import Tripwire
from tests.support.voice_tickets import seal_ticket, ticket_key

TOKEN = "p7-twilio-auth-token-0123456789"
FORM = {"CallSid": "CAin", "CallStatus": "completed"}


class Alerts:
    def __init__(self) -> None:
        self.posted: list = []

    async def post(self, text: str) -> None:
        self.posted.append(text)


async def test_no_companion_releases_ticket_and_marker_and_alerts():
    db, touched, alerts = EvalDb(), [], Alerts()
    store = FirestoreEphemeralStore(db, collection="voice")

    async def no_companion(user_id):
        return None

    async def twilio(url, form_params, signature):
        return verify_twilio_signature(TOKEN, url, form_params, signature)

    cipher = AesGcmTestCipher()
    app = Quart("voice_webhook_c1")
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=Tripwire("user_repository", touched), ephemeral_store=store, alert_sink=alerts,
        notification_service=Tripwire("notification_service", touched), lelik_agent_provider=no_companion,
        answer_url="https://app.example/voice/answer", signature_verifier=twilio, cipher=cipher, digest_key=b"k" * 32))
    with eval_transactions():
        await store.set("voice_pending_callback:CAin", {
            "ticket_ct": await seal_ticket(cipher, "voice_pending_callback:CAin", "t1"),
            "user_id": "u1", "account_id": "a1", "to_number": "+12025550100"}, ttl_s=300)
        await store.set(ticket_key("t1"), {"user_id": "u1", "account_id": "a1"}, ttl_s=300)
        await store.set("voice_one_call:u1", {"in_flight": True}, ttl_s=300)
        signature = RequestValidator(TOKEN).compute_signature("http://localhost/voice/inbound-status", FORM)
        resp = await app.test_client().post("/voice/inbound-status", form=FORM,
                                            headers={"X-Twilio-Signature": signature})
    assert resp.status_code == 200
    assert db.collection("voice").docs == {}             # pending claim, ticket and marker all gone
    assert len(alerts.posted) == 1 and "not configured" in alerts.posted[0]
    assert touched == []                                 # no "calling you back" message to the chat
