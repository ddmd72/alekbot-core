"""
Cloud Run Job entrypoint — deep research execution.

Triggered by ClaudeDeepResearchAdapter.create_interaction() via Cloud Run Jobs API.
Runs independently of Cloud Tasks deadlines; task-timeout is configured at the job level.

Environment variables (static — set at job deploy time):
  ANTHROPIC_API_KEY         Secret: Anthropic API key.
  GOOGLE_CLOUD_PROJECT      GCP project ID.
  CLOUD_RUN_SERVICE_URL     Base URL of the Cloud Run service (for Cloud Tasks delivery).
  APP_ENV                   "development" | "production" (determines queue suffix).
  DEBUG_PROMPTS             "true" | "false" — enables GCS debug prompt uploads.
  DEBUG_PROMPTS_BUCKET      GCS bucket name for debug uploads.
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

from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.agents.claude_deep_research_runner_agent import ClaudeDeepResearchRunnerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.infrastructure.agent_manifest import Intent
from src.services.deep_research_delivery import deliver_deep_research
from src.utils.logger import logger


def _build_task_queue() -> GcpTaskQueue:
    """Build the Cloud Tasks queue for DocPlanner delivery."""
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    service_url = os.environ["CLOUD_RUN_SERVICE_URL"]
    env = os.environ.get("APP_ENV", "development")
    queue_suffix = "dev" if env == "development" else "prod"
    return GcpTaskQueue(
        project_id=project,
        location="us-central1",
        queue_name=f"agent-tasks-{queue_suffix}",
        service_url=service_url,
        service_account_email=None,  # unauthenticated — service is allow-unauthenticated
    )


async def main() -> None:
    query = os.environ["JOB_QUERY"]
    context = json.loads(os.environ["JOB_CONTEXT_JSON"])

    user_id = context.get("user_id", "")
    logger.info(
        "[ResearchJob] Starting: user=%s query_len=%d model=%s",
        user_id[:8], len(query), context.get("model", "?"),
    )

    anthropic_client = anthropic.AsyncAnthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"]
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
        sys.exit(1)

    result = response.result or {}
    result_text = result.get("text", "")
    if not result_text:
        logger.error("[ResearchJob] Agent returned empty result — aborting delivery")
        sys.exit(1)

    try:
        await deliver_deep_research(
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
        )
    except Exception as exc:
        logger.error(
            "[ResearchJob] Delivery failed: user=%s error=%s",
            user_id[:8], exc, exc_info=True,
        )
        sys.exit(1)

    logger.info("[ResearchJob] Completed and delivered: user=%s", user_id[:8])


if __name__ == "__main__":
    asyncio.run(main())
