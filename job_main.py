"""
Cloud Run Job entrypoint — deep research execution.

Triggered by ClaudeDeepResearchAdapter.create_interaction() via Cloud Run Jobs API.
Runs independently of Cloud Tasks deadlines; task-timeout is configured at the job level.

Environment variables (static — set at job deploy time):
  ANTHROPIC_API_KEY         Secret: Anthropic API key.
  GOOGLE_CLOUD_PROJECT      GCP project ID.
  CLOUD_RUN_SERVICE_URL     Base URL of the Cloud Run service (for Cloud Tasks delivery).
  APP_ENV                   "development" | "production" (determines queue suffix).
  DEBUG_PROMPTS             "true" | "false" — gates the BigQuery prompt-content store.
  GCS_MEDIA_BUCKET          GCS bucket for raw research result uploads (optional).
  DEEP_RESEARCH_SECOND_PASS "true" | "false" — enable/disable second-pass critic (fallback).

Environment variables (per-run — injected as overrides by CloudRunJobsAdapter):
  JOB_QUERY        Research query (full text, may include critic query for second pass).
  JOB_CONTEXT_JSON JSON-encoded context dict:
                     user_id, account_id, original_query, system_prompt, model,
                     job_id, session_id.

Exit codes:
  0 — research completed and delivery task enqueued successfully.
  1 — research failed or delivery failed (Cloud Run Job marks task as failed).
"""
import asyncio
import json
import os
import sys

import anthropic

from src.adapters.bigquery_prompt_content_adapter import BigQueryPromptContentAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.agents.claude_deep_research_runner_agent import ClaudeDeepResearchRunnerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.billing import calculate_cost
from src.infrastructure.agent_manifest import Intent
from src.services.deep_research_delivery import deliver_deep_research
from src.utils.logger import logger, quiet_third_party_loggers

quiet_third_party_loggers()
from src.composition.memory_telemetry_setup import build_memory_telemetry
from src.domain.memory_telemetry import SCOPE_WORKER_TASK
from src.utils.memory_scope import memory_scope
from src.utils.telemetry import init_telemetry


def _build_account_repo() -> FirestoreAccountRepository:
    """Build account repo for billing recording. Uses same database as main service."""
    from google.cloud import firestore as _firestore
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    db = _firestore.AsyncClient(project=project, database="us-production")
    env = os.environ.get("APP_ENV", "development")
    prefix = "development_" if env == "development" else ""
    return FirestoreAccountRepository(db_client=db, collection_name=f"{prefix}domain_accounts_v2")


async def _record_billing(account_id: str, model: str, result: dict) -> None:
    """Record deep research token usage to Firestore. Best-effort, non-raising."""
    if not account_id or not model:
        return
    total_tokens = result.get("total_tokens", 0)
    cache_read_tokens = result.get("cache_read_tokens", 0)
    cache_write_tokens = result.get("cache_write_tokens", 0)
    if not (total_tokens or cache_read_tokens or cache_write_tokens):
        return
    # Price input and output at their own rates. Collapsing both into prompt_tokens
    # (the old bug) billed output — which dominates a research report — at the cheap
    # input rate (e.g. Sonnet $3/M instead of $15/M).
    cost = calculate_cost(
        model=model,
        prompt_tokens=result.get("prompt_tokens", 0),
        completion_tokens=result.get("completion_tokens", 0),
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_write_tokens,
    )
    try:
        repo = _build_account_repo()
        await repo.increment_account_usage(account_id=account_id, tokens=total_tokens, cost=cost)
        logger.info(
            "[ResearchJob] Billing recorded: account=%s model=%s tokens=%d cache_read=%d cost=$%.4f",
            account_id[:20], model, total_tokens, cache_read_tokens, cost,
        )
    except Exception as exc:
        logger.error("[ResearchJob] Billing recording failed (non-fatal): %s", exc, exc_info=True)


def _build_task_queue() -> GcpTaskQueue:
    """Build the Cloud Tasks queue for the HtmlPageGenerator delivery task.

    SERVICE_ACCOUNT_EMAIL is mandatory: the /worker route enforces a Google OIDC
    gate (src/web/worker_oidc_verifier.py), so the delivery task MUST carry an
    oidc_token minted as that SA — otherwise /worker answers 401 and the research
    result is never delivered. Symmetric with the main service's enqueue side
    (main.py builds GcpTaskQueue with the same secret).
    """
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    service_url = os.environ["CLOUD_RUN_SERVICE_URL"]
    env = os.environ.get("APP_ENV", "development")
    queue_suffix = "dev" if env == "development" else "prod"
    return GcpTaskQueue(
        project_id=project,
        location="us-central1",
        queue_name=f"agent-tasks-{queue_suffix}",
        service_url=service_url,
        service_account_email=os.environ.get("SERVICE_ACCOUNT_EMAIL"),
    )


async def _report_failure(task_queue, context: dict, query: str, reason: str) -> None:
    """Ask the main service to tell the user the research failed; never raises (the job exits 1 next)."""
    session_id = context.get("session_id", "")
    try:
        await task_queue.enqueue_worker_task(
            "deep_research_failed",
            {
                "user_id": context.get("user_id", ""),
                "account_id": context.get("account_id", ""),
                "session_id": session_id,
                "origin_platform": context.get("origin_platform") or "",
                "query": (context.get("original_query") or query)[:200],
                "reason": reason,
            },
            # A job retry must not notify twice.
            dedup_key=f"deep_research_failed:{context.get('job_id') or session_id}",
        )
    except Exception as exc:
        logger.error("[ResearchJob] Failure notice not enqueued: reason=%s error=%s", reason, exc, exc_info=True)


async def _capture_research_result(
    result: dict, result_text: str, user_id: str, context: dict
) -> None:
    """Durably persist deep-research output(s) to BigQuery. Best-effort, non-raising."""
    # DEBUG_PROMPTS is the global capture switch (write / don't write).
    if os.environ.get("DEBUG_PROMPTS", "false").lower() != "true":
        return
    dataset = os.environ.get("BIGQUERY_PROMPT_DATASET", "")
    if not dataset:
        return
    try:
        store = BigQueryPromptContentAdapter(
            dataset=dataset,
            table=os.environ.get("BIGQUERY_PROMPT_TABLE", "prompt_content"),
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        )
        query_text = result.get("query", context.get("original_query", ""))
        model = result.get("model", "")
        account_id = context.get("account_id", "")
        job_id = context.get("job_id")
        total_tokens = result.get("total_tokens", 0)
        second_pass = result.get("second_pass", False)
        round1 = result.get("round1_text", "")
        if round1:
            await store.record_dr_result(
                output_text=round1, query=query_text,
                user_id=user_id, account_id=account_id,
                model=model, provider="claude", source="claude_job",
                job_id=job_id, pass_index=1, total_tokens=total_tokens,
            )
        await store.record_dr_result(
            output_text=result_text, query=query_text,
            user_id=user_id, account_id=account_id,
            model=model, provider="claude", source="claude_job",
            job_id=job_id, pass_index=2 if second_pass else None,
            total_tokens=total_tokens,
        )
    except Exception as exc:
        logger.error("[ResearchJob] BigQuery content capture failed: %s", exc, exc_info=True)


async def main() -> None:
    # Without this the job runs on the default no-op provider and is invisible
    # to tracing — the deep-research path was the one blind spot in the stack.
    init_telemetry("alek-research-job")

    memory_telemetry = build_memory_telemetry("alek-research-job")
    if memory_telemetry:
        await memory_telemetry.start()
    try:
        with memory_scope(SCOPE_WORKER_TASK, "deep_research_job"):
            await _run_job()
    finally:
        if memory_telemetry:
            memory_telemetry.sample_once()  # last look before the process exits
            await memory_telemetry.stop()


async def _run_job() -> None:
    query = os.environ["JOB_QUERY"]
    context = json.loads(os.environ["JOB_CONTEXT_JSON"])

    user_id = context.get("user_id", "")
    logger.info(
        "[ResearchJob] Starting: user=%s query_len=%d model=%s",
        user_id[:8], len(query), context.get("model", "?"),
    )

    # Same timeout as ClaudeAdapter: read=120s protects against documented Anthropic
    # mid-stream-stall bug (stream stalls without raising, defaults to 600s then).
    anthropic_client = anthropic.AsyncAnthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=anthropic.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
    )
    task_queue = _build_task_queue()

    # GCS media storage for raw research result uploads
    media_bucket = os.environ.get("GCS_MEDIA_BUCKET")
    media_storage = GcsMediaAdapter(media_bucket) if media_bucket else None

    agent = ClaudeDeepResearchRunnerAgent(
        config=AgentConfig(
            agent_id=f"claude_deep_research_runner_{user_id}",
            agent_type="claude_deep_research_runner",
            timeout_ms=None,  # no Python-level timeout — job task-timeout is the ceiling
            capabilities=["execute_deep_research_claude"],
        ),
        anthropic_client=anthropic_client,
    )

    message = AgentMessage.create(
        sender="job",
        recipient=agent.agent_id,
        intent=AgentIntent.DELEGATE,
        payload={"query": query, "intent": Intent.EXECUTE_DEEP_RESEARCH_CLAUDE},
        context=context,
    )

    response = await agent.execute(message)

    if response.status != AgentStatus.SUCCESS:
        logger.error(
            "[ResearchJob] Agent failed: user=%s error=%s",
            user_id[:8], response.error,
        )
        await _report_failure(task_queue, context, query, "agent_failed")
        sys.exit(1)

    result = response.result or {}
    result_text = result.get("text", "")
    if not result_text:
        logger.error("[ResearchJob] Agent returned empty result — aborting delivery")
        await _report_failure(task_queue, context, query, "empty_result")
        sys.exit(1)

    # Durable content capture BEFORE delivery — deep research is expensive; if
    # delivery later fails, the result is already persisted in BigQuery. Both
    # passes are captured: the first pass never reaches the user (it feeds the
    # critic) but matters for history. No-op when BIGQUERY_PROMPT_DATASET unset.
    await _capture_research_result(result, result_text, user_id, context)

    # Record billing BEFORE delivery — the token cost was incurred during research,
    # not delivery. Delivery can fail (e.g. OIDC gate) and exit(1); billing must not
    # be skipped when it does, otherwise expensive DR usage goes uncounted.
    await _record_billing(
        account_id=context.get("account_id", ""),
        model=result.get("model", ""),
        result=result,
    )

    # Channel comes from the per-channel session_id ("user_id:channel_id"), same rule as the webhook.
    session_id = context.get("session_id", "")
    origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None
    origin_platform = context.get("origin_platform") or None  # absent on pre-deploy jobs

    try:
        handed_off = await deliver_deep_research(
            result_text=result_text,
            user_id=user_id,
            account_id=context.get("account_id", ""),
            query=result.get("query", context.get("original_query", "")),
            task_queue=task_queue,
            session_id=context.get("session_id", ""),
            round1_text=result.get("round1_text", ""),
            media_storage=media_storage,
            model=result.get("model", ""),
            total_tokens=result.get("total_tokens", 0),
            second_pass=result.get("second_pass", False),
            channel_id_override=origin_channel_id,
            platform_override=origin_platform,
            report_dedup_id=context.get("job_id"),
        )
        if handed_off is False:  # the report task could not be enqueued (swallowed inside)
            raise RuntimeError("report task not enqueued")
    except Exception as exc:
        logger.error(
            "[ResearchJob] Delivery failed: user=%s error=%s",
            user_id[:8], exc, exc_info=True,
        )
        await _report_failure(task_queue, context, query, "delivery_failed")
        sys.exit(1)

    logger.info("[ResearchJob] Completed and delivered: user=%s", user_id[:8])


if __name__ == "__main__":
    asyncio.run(main())
