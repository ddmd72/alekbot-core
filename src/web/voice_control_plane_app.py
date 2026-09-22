"""
Voice control-plane endpoints — main service.

Two Quart routes consumed by the relay side (`CallControlPlanePort` /
`HttpCallControlPlaneAdapter`, Task 6):

- `POST /voice/session-config` — relay resolves an opaque call ticket
  (minted by the auth webhook, a later task) into the realtime session
  config (instructions + identity) via `EphemeralStore`.
- `POST /voice/submit-transcript` — relay reports end-of-call usage +
  transcript. This records per-model usage/cost, pricing via
  `domain.billing.calculate_realtime_cost` on the already-flattened leg
  counts `OpenAIRealtimeAdapter._flatten_usage` produced (Task 16), hands
  the transcript (plus `user_id`/`account_id`, needed by the real Task 15
  consumer to run extraction and deliver the summary) to the
  injected `summary_consumer`, and releases the one-call-per-user marker
  (`voice_one_call:{user_id}`) written by the auth webhook before dialing
  out — this MUST happen even if usage recording or the summary consumer
  raises, since a stuck marker would permanently lock the user out of ever
  calling again.

Both routes are OIDC-protected the same way `/worker` is (see
`src/web/worker_oidc_verifier.py`): the verifier is injected as an async
callable rather than imported directly, so the route is testable without
a real Google token and the local-dev bypass policy stays in main.py's
wiring, not duplicated here.
"""
from datetime import datetime

from quart import Blueprint, Response, jsonify, request

from src.domain.billing import calculate_realtime_cost
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart
from src.domain.request_context import RequestContext
from src.utils.logger import logger


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

        try:
            for model, tokens in body.get("usage_by_model", {}).items():
                try:
                    cost = calculate_realtime_cost(model, tokens) if isinstance(tokens, dict) else 0.0
                    logger.info(f"voice call {call_id}: model={model} tokens={tokens} cost={cost}")
                    total_tokens = sum(tokens.values()) if isinstance(tokens, dict) else tokens
                    await quota_service.record_usage(account_id, model, total_tokens, cost)
                except Exception:
                    logger.error(
                        f"voice call {call_id}: usage recording failed for model={model}",
                        exc_info=True,
                    )

            try:
                await summary_consumer(
                    call_id=call_id,
                    user_id=user_id,
                    account_id=account_id,
                    transcript_text=body["transcript_text"],
                    turns=body.get("turns", []),
                )
            except Exception:
                logger.error(f"voice call {call_id}: summary consumer failed", exc_info=True)

            try:
                turns = body.get("turns", [])
                if turns:
                    # Exactly one realtime provider in Slice 1 (RFC §4.3) — not a
                    # placeholder, a real hardcoded value.
                    provider = "openai"
                    model = next(iter(body.get("usage_by_model", {})), "")
                    with RequestContext(user_id=user_id, account_id=account_id):
                        for turn_index, turn_segment in enumerate(turns):
                            started = datetime.fromisoformat(turn_segment["started_at"])
                            ended = datetime.fromisoformat(turn_segment["ended_at"])
                            latency_ms = (ended - started).total_seconds() * 1000
                            request_obj = LLMRequest(
                                model_name=model,
                                messages=[
                                    Message(
                                        role="user",
                                        parts=[MessagePart(text=turn_segment["request_text"])],
                                    )
                                ],
                            )
                            response_obj = LLMResponse(text=turn_segment["response_text"])
                            await prompt_content_store.record_turn(
                                request=request_obj,
                                response=response_obj,
                                agent_id=f"lelik_agent_{user_id}",
                                agent_type="lelik",
                                account_id=account_id,
                                turn=turn_index,
                                latency_ms=latency_ms,
                                provider=provider,
                            )
            except Exception:
                logger.error(f"voice call {call_id}: turn content recording failed", exc_info=True)
        finally:
            # Release the one-call-per-user marker (written by the auth webhook
            # before dialing out) regardless of usage-recording or
            # summary-consumer outcome — a stuck marker would permanently lock
            # the user out of ever calling again. Covers the whole
            # post-authentication body, not just the summary_consumer call, so
            # e.g. a malformed usage_by_model payload or a future QuotaService
            # that raises can never leave the marker stuck either.
            await ephemeral_store.delete(f"voice_one_call:{user_id}")

        return jsonify({"ok": True}), 200

    return bp
