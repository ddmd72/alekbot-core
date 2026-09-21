"""
Voice control-plane endpoints — main service.

Two Quart routes consumed by the relay side (`CallControlPlanePort` /
`HttpCallControlPlaneAdapter`, Task 6):

- `POST /voice/session-config` — relay resolves an opaque call ticket
  (minted by the auth webhook, a later task) into the realtime session
  config (instructions + identity) via `EphemeralStore`.
- `POST /voice/submit-transcript` — relay reports end-of-call usage +
  transcript. This records per-model usage/cost (pricing itself is a Task
  16 forward reference — `_price_realtime_usage` is a placeholder here),
  hands the transcript to the injected `summary_consumer` (Task 15 wires
  the real one), and releases the one-call-per-user marker
  (`voice_one_call:{user_id}`) written by the auth webhook before dialing
  out — this MUST happen even if the summary consumer fails, since a stuck
  marker would permanently lock the user out of ever calling again.

Both routes are OIDC-protected the same way `/worker` is (see
`src/web/worker_oidc_verifier.py`): the verifier is injected as an async
callable rather than imported directly, so the route is testable without
a real Google token and the local-dev bypass policy stays in main.py's
wiring, not duplicated here.
"""
from quart import Blueprint, Response, jsonify, request

from src.utils.logger import logger


def _price_realtime_usage(model: str, tokens: dict) -> float:
    """Placeholder pricing hook — Task 16 implements real per-model realtime
    pricing. Returns 0.0 so usage is still recorded (with a real token count)
    while cost accounting is wired up."""
    return 0.0


def create_voice_control_plane_blueprint(
    ephemeral_store,
    quota_service,
    prompt_content_store,
    summary_consumer,
    oidc_verifier,
) -> Blueprint:
    bp = Blueprint("voice_control_plane", __name__)

    async def _verify_or_401() -> Response | None:
        auth_header = request.headers.get("Authorization", "")
        if not await oidc_verifier(auth_header):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @bp.route("/voice/session-config", methods=["POST"])
    async def session_config():
        unauthorized = await _verify_or_401()
        if unauthorized:
            return unauthorized
        body = await request.get_json()
        ticket = body["ticket"]
        config = await ephemeral_store.get(f"voice_ticket:{ticket}")
        if config is None:
            return jsonify({"error": "unknown or expired ticket"}), 404
        return jsonify(config), 200

    @bp.route("/voice/submit-transcript", methods=["POST"])
    async def submit_transcript():
        unauthorized = await _verify_or_401()
        if unauthorized:
            return unauthorized
        body = await request.get_json()
        call_id = body["call_id"]
        user_id = body["user_id"]
        account_id = body["account_id"]

        for model, tokens in body.get("usage_by_model", {}).items():
            cost = _price_realtime_usage(model, tokens)  # Task 16 implements this
            logger.info(f"voice call {call_id}: model={model} tokens={tokens} cost={cost}")
            total_tokens = sum(tokens.values()) if isinstance(tokens, dict) else tokens
            await quota_service.record_usage(account_id, model, total_tokens, cost)

        try:
            await summary_consumer(
                call_id=call_id,
                transcript_text=body["transcript_text"],
                turns=body.get("turns", []),
            )
        except Exception:
            logger.error(f"voice call {call_id}: summary consumer failed", exc_info=True)
        finally:
            # Release the one-call-per-user marker (written by the auth webhook
            # before dialing out) regardless of summary-consumer outcome — a
            # stuck marker would permanently lock the user out of ever calling
            # again.
            await ephemeral_store.delete(f"voice_one_call:{user_id}")

        return jsonify({"ok": True}), 200

    return bp
