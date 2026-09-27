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
  copy). When the body names the delegation (`ticket` + `call_id`), the
  result is also kept for `_LATE_ANSWER_TTL_S` so it can reach chat if the
  relay stopped waiting for it.
- `POST /voice/delegate/abandon` — the relay stopped waiting for one
  delegation (its 300 s timeout, or the call ended while it was pending).
  The answer then goes to the user's chat through the injected
  `late_answer_sink`, exactly once, whichever side gets there first: both
  routes write their own marker first (result / abandoned), then the
  abandoned side is checked, and the post is claimed only by an atomic
  `get_and_delete` on the result key.

All four routes are OIDC-protected the same way `/worker` is (see
`src/web/worker_oidc_verifier.py`): the verifier is injected as an async
callable rather than imported directly, so the route is testable without
a real Google token and the local-dev bypass policy stays in main.py's
wiring, not duplicated here.
"""
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from quart import Blueprint, Response, jsonify, request

from src.domain.billing import calculate_realtime_cost
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart
from src.domain.request_context import RequestContext
from src.domain.voice_amd import is_voicemail
from src.utils.logger import logger
from src.utils.telemetry import set_request_context, start_span

# Long enough to cover the relay's 300 s wait plus a slow delegation finishing after it.
_LATE_ANSWER_TTL_S = 900


def _result_key(ticket: str, call_id: str) -> str:
    return f"voice_delegation_result:{ticket}:{call_id}"


def _abandoned_key(ticket: str, call_id: str) -> str:
    return f"voice_delegation_abandoned:{ticket}:{call_id}"


if TYPE_CHECKING:  # type-only: web/ must not import agents/ at runtime (REQ-ARCH-15)
    from src.agents.lelik_agent import LelikAgent


def create_voice_control_plane_blueprint(
    ephemeral_store,
    quota_service,
    prompt_content_store,
    summary_consumer,
    oidc_verifier,
    alert_sink,
    lelik_agent_provider: Callable[[str], Awaitable[Optional["LelikAgent"]]] = None,
    one_call_ttl_s: int = 3600,
    late_answer_sink: Optional[Callable[..., Awaitable[None]]] = None,
) -> Blueprint:
    """`late_answer_sink(user_id=, account_id=, request=, output=)` posts an answer the relay
    stopped waiting for to the user's chat; without it such answers are kept but never posted."""
    bp = Blueprint("voice_control_plane", __name__)

    async def _post_claimed(ticket: str, call_id: str) -> None:
        # The only claim: two racing callers cannot both get the record back.
        claimed = await ephemeral_store.get_and_delete(_result_key(ticket, call_id))
        if claimed is None:
            return
        if late_answer_sink is None:
            logger.warning(f"voice call {ticket}: late answer to {call_id} dropped, no sink configured")
            return
        await late_answer_sink(
            user_id=claimed["user_id"], account_id=claimed["account_id"],
            request=claimed.get("request", ""), output=claimed["output"],
        )
        logger.info(f"voice call {ticket}: late answer to {call_id} posted to chat")

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
        # The one-call-per-user marker was written at the short setup-window TTL
        # (`VoiceCallSetupService.claim`, e.g. 300s) so a caller who never gets this far never
        # locks themselves out for an hour. The call is *actually* live only once the relay
        # redeems the ticket here, so this is where the marker earns its full-call TTL. Best
        # effort: a failure here must never turn a working call into a failed session-config
        # response — the marker just keeps its short TTL and, worst case, expires mid-call
        # (recoverable by the user; a stuck long-lived marker from a bug here would not be).
        try:
            user_id = config.get("user_id")
            marker_key = f"voice_one_call:{user_id}"
            marker = await ephemeral_store.get(marker_key)
            if marker is not None:
                await ephemeral_store.set(marker_key, marker, ttl_s=one_call_ttl_s)
        except Exception:
            logger.error(f"voice session-config: extending the one-call marker failed for ticket {ticket}",
                        exc_info=True)
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

            answered_by = None
            try:
                amd = await ephemeral_store.get(f"voice_amd:{call_id}")
                answered_by = amd.get("answered_by") if isinstance(amd, dict) else None
            except Exception:
                # Unknown verdict = live call: losing a real summary is worse than keeping voicemail's.
                logger.error(f"voice call {call_id}: AMD verdict lookup failed", exc_info=True)
            caller_texts = [turn.get("request_text", "") for turn in body.get("turns", [])]
            voicemail = is_voicemail(answered_by, caller_texts)
            if voicemail:
                # RFC §4.6: a voicemail greeting must never become a memory.
                logger.info(f"voice call {call_id}: voicemail ({answered_by}), summary skipped")
            try:
                if not voicemail:
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
            # voice_amd:{call_id} is left to its TTL: nothing reads it after this point.

        return jsonify({"ok": True}), 200

    @bp.route("/voice/delegate", methods=["POST"])
    async def delegate():
        unauthorized = await _verify_or_401()
        if unauthorized:
            return unauthorized
        body = await request.get_json()
        user_id, account_id = body["user_id"], body["account_id"]
        set_request_context(user_id=user_id)
        arguments = body.get("arguments") or {}
        # Groups one phone-call tool call's delegation spans under a named root in Logfire.
        with start_span("voice.delegate", {
            "voice.delegate.user_id": user_id,
            "voice.delegate.intent": arguments.get("intent"),
        }):
            try:
                async with RequestContext(user_id=user_id, account_id=account_id):
                    agent = await lelik_agent_provider(user_id) if lelik_agent_provider else None
                    if agent is None:
                        return jsonify({"error": "voice companion not configured"}), 503
                    output = await agent.delegate(
                        user_id=user_id, account_id=account_id,
                        arguments=arguments, call_context=body.get("call_context") or [],
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
        ticket, call_id = body.get("ticket"), body.get("call_id")
        # Only a real answer is kept: an empty one has nothing to post.
        if ticket and call_id and output:
            # Result first, abandoned check second — the abandon route writes and reads in the
            # opposite order, so whichever lands second always sees the other's marker.
            try:
                await ephemeral_store.set(_result_key(ticket, call_id), {
                    "output": output, "request": body.get("request", ""),
                    "user_id": user_id, "account_id": account_id,
                }, ttl_s=_LATE_ANSWER_TTL_S)
                if await ephemeral_store.get(_abandoned_key(ticket, call_id)) is not None:
                    await _post_claimed(ticket, call_id)
            except Exception:
                # The relay may still be waiting: its answer must not turn into an error.
                logger.error(f"voice call {ticket}: keeping/posting the answer to {call_id} failed", exc_info=True)
        return jsonify({"output": output}), 200

    @bp.route("/voice/delegate/abandon", methods=["POST"])
    async def abandon_delegation():
        unauthorized = await _verify_or_401()
        if unauthorized:
            return unauthorized
        body = await request.get_json()
        ticket, call_id = body["ticket"], body["call_id"]
        logger.info(f"voice call {ticket}: relay abandoned delegation {call_id}")
        try:
            await ephemeral_store.set(_abandoned_key(ticket, call_id), {"abandoned": True},
                                      ttl_s=_LATE_ANSWER_TTL_S)
            await _post_claimed(ticket, call_id)
        except Exception:
            logger.error(f"voice call {ticket}: posting the abandoned answer to {call_id} failed", exc_info=True)
        return jsonify({"ok": True}), 200

    return bp
