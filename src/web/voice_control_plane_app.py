"""
Voice control-plane endpoints — main service.

Three Quart routes consumed by the relay side (`CallControlPlanePort` /
`HttpCallControlPlaneAdapter`, Task 6/7):

- `POST /voice/session-config` — relay resolves an opaque call ticket
  (minted by the auth webhook, a later task) into the realtime session
  config (instructions + identity) via `EphemeralStore`. The ticket is
  **atomically consumed on resolution** (`get_and_delete`, one Firestore
  transaction) — single use, so a captured ticket cannot be replayed within
  its TTL to read back the owner's persona and biographical facts, and two
  concurrent redemptions cannot both succeed.
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
- `POST /voice/delegate` — relay forwards one `delegate_to_specialist` tool
  call raised inside the live realtime session (RFC §4.7). Runs it through
  `LelikAgent.delegate` (the same `DelegationEngine` path a text orchestrator
  uses), inside a `RequestContext` scoped to the call's user/account, and
  returns the specialist's text result for the relay to speak back. No
  retry — a retry would re-run Alek's pipeline (double spend, double chat
  copy).

All three routes are OIDC-protected the same way `/worker` is (see
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
from src.utils.telemetry import set_request_context


def create_voice_control_plane_blueprint(
    ephemeral_store,
    quota_service,
    prompt_content_store,
    summary_consumer,
    oidc_verifier,
    alert_sink,
    lelik_agent_provider=None,
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
        # Single use, and atomically so. Until the ticket was consumed here it
        # stayed redeemable for its whole TTL (`voice_webhook_app.
        # _TICKET_TTL_S`, 300s) — a replayable bearer credential for an
        # endpoint that hands back the owner's assembled persona, biographical
        # facts and standing directives. `get_and_delete` rather than
        # `get()` + `delete()` because the latter is two round trips: two
        # concurrent requests for the same ticket could both read it before
        # either delete landed, and both be served. The relay fetches the
        # config exactly once per call
        # (`HttpCallControlPlaneAdapter.fetch_session_config`), so single use
        # costs the legitimate caller nothing.
        config = await ephemeral_store.get_and_delete(f"voice_ticket:{ticket}")
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
            except Exception as exc:
                # An alert, not just a log line. This `except` is the last stop
                # for the entire end-of-call summary pipeline
                # (CompanionExtractorRunner -> LelikSummarizerAgent ->
                # notify_call_summary), and its silent-failure mode is
                # invisible from the outside: the call completes normally, the
                # usage is billed, and simply nothing ever reaches chat or
                # memory. That is exactly what a missing Firestore prompt
                # artefact for `lelik_summarizer` produced (build_for_agent
                # fails closed by repo convention -> AgentResponse.failure ->
                # runner raises -> here). The artefacts are fixed, but the
                # failure CLASS is permanent — any future missing/broken prompt
                # lands in this same block.
                logger.error(f"voice call {call_id}: summary consumer failed", exc_info=True)
                try:
                    await alert_sink.post(
                        f"Voice: end-of-call summary failed for call {call_id} "
                        f"(user {user_id}) — nothing delivered to chat or memory: {exc}"
                    )
                except Exception:
                    logger.error(f"voice call {call_id}: summary failure alert failed", exc_info=True)

            try:
                turns = body.get("turns", [])
                if turns:
                    # Exactly one realtime provider in Slice 1 (RFC §4.3) — not a
                    # placeholder, a real hardcoded value.
                    provider = "openai"
                    model = next(iter(body.get("usage_by_model", {})), "")
                    # BigQueryPromptContentAdapter._build_record reads user_id via
                    # src.utils.telemetry.get_request_context(), not from a
                    # parameter — a separate ContextVar system from
                    # src.domain.request_context.RequestContext (which only
                    # backs Firestore tenancy resolution). No reset afterward:
                    # each Quart request runs its own asyncio Task, matching
                    # the only other real call site (slack/http_adapter.py).
                    set_request_context(user_id=user_id)
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
            # Every write above (turns, and the summarizer's own LLM turn) is scheduled in
            # the background. Cloud Run throttles the CPU once this response is sent, and
            # writes left pending starved for minutes, then failed with SSL EOF (2026-09-23).
            try:
                await prompt_content_store.flush()
            except Exception:
                logger.error(f"voice call {call_id}: prompt content flush failed", exc_info=True)
            # Release the one-call-per-user marker (written by the auth webhook
            # before dialing out) regardless of usage-recording or
            # summary-consumer outcome — a stuck marker would permanently lock
            # the user out of ever calling again. Covers the whole
            # post-authentication body, not just the summary_consumer call, so
            # e.g. a malformed usage_by_model payload or a future QuotaService
            # that raises can never leave the marker stuck either.
            await ephemeral_store.delete(f"voice_one_call:{user_id}")

        return jsonify({"ok": True}), 200

    @bp.route("/voice/delegate", methods=["POST"])
    async def delegate():
        unauthorized = await _verify_or_401()
        if unauthorized:
            return unauthorized
        body = await request.get_json()
        user_id, account_id = body["user_id"], body["account_id"]
        set_request_context(user_id=user_id)
        try:
            async with RequestContext(user_id=user_id, account_id=account_id):
                agent = await lelik_agent_provider(user_id) if lelik_agent_provider else None
                if agent is None:
                    return jsonify({"error": "voice companion not configured"}), 503
                output = await agent.delegate(
                    user_id=user_id, account_id=account_id,
                    arguments=body.get("arguments") or {}, call_context=body.get("call_context") or [],
                )
        except Exception:
            logger.error(f"voice delegate failed for user {user_id}", exc_info=True)
            return jsonify({"error": "delegation failed"}), 500
        finally:
            # Same throttled-CPU-after-response hazard submit_transcript's flush guards
            # against (see its comment above) — this path schedules background BigQuery
            # writes too (every specialist LLM call the delegation reaches).
            try:
                await prompt_content_store.flush()
            except Exception:
                logger.error(f"voice delegate for user {user_id}: prompt content flush failed", exc_info=True)
        return jsonify({"output": output}), 200

    return bp
