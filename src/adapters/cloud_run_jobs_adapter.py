"""
CloudRunJobsAdapter — JobRunnerPort implementation backed by Cloud Run Jobs v2 REST API.

Triggers a Cloud Run Job execution with per-run environment variable overrides and
returns immediately (fire-and-forget). The job runs asynchronously with its own
task-timeout (up to 168 hours), completely decoupled from Cloud Tasks deadlines.

Authentication: Application Default Credentials (metadata server on Cloud Run,
ADC on local dev). Token is refreshed synchronously in a thread pool to keep
the async event loop unblocked.

REST endpoint:
  POST https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run
  Body: {"overrides": {"containerOverrides": [{"env": [{"name": ..., "value": ...}]}]}}
"""
import asyncio

import aiohttp
import google.auth
import google.auth.transport.requests

from ..ports.job_runner_port import JobRunnerPort
from ..utils.logger import logger

_CLOUD_RUN_API = "https://run.googleapis.com/v2"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class CloudRunJobsAdapter(JobRunnerPort):
    """
    Triggers Cloud Run Job executions via the v2 REST API.

    One instance per process — stateless except for project/region config.
    """

    def __init__(self, project: str, region: str) -> None:
        """
        Args:
            project: GCP project ID (e.g. "my-project-123").
            region:  Cloud Run region (e.g. "us-central1").
        """
        self._project = project
        self._region = region
        logger.info(
            "✅ [CloudRunJobsAdapter] Initialized: project=%s region=%s",
            project, region,
        )

    # ------------------------------------------------------------------
    # JobRunnerPort interface
    # ------------------------------------------------------------------

    async def run_job(
        self,
        job_name: str,
        env_overrides: dict[str, str],
    ) -> str:
        """
        Trigger a Cloud Run Job execution with per-run env var overrides.

        Returns the operation name (Cloud Run execution identifier).
        Does NOT wait for the job to complete.
        """
        token = await self._get_access_token()
        url = (
            f"{_CLOUD_RUN_API}/projects/{self._project}"
            f"/locations/{self._region}/jobs/{job_name}:run"
        )
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "env": [
                            {"name": k, "value": v}
                            for k, v in env_overrides.items()
                        ]
                    }
                ]
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if not resp.ok:
                    error_text = await resp.text()
                    raise RuntimeError(
                        f"[CloudRunJobsAdapter] run_job failed: "
                        f"status={resp.status} body={error_text[:300]}"
                    )
                data = await resp.json()

        operation_name: str = data.get("name", "")
        # Extract short execution ID for logging (last path segment).
        exec_id = operation_name.rsplit("/", 1)[-1] if operation_name else "unknown"
        logger.info(
            "[CloudRunJobsAdapter] Job execution triggered: job=%s exec=%s",
            job_name, exec_id,
        )
        return operation_name

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _get_access_token() -> str:
        """
        Fetch a GCP access token using Application Default Credentials.

        Runs the synchronous google-auth refresh in a thread pool so the
        async event loop is not blocked.
        """
        def _refresh() -> str:
            credentials, _ = google.auth.default(scopes=_SCOPES)
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            return credentials.token

        return await asyncio.to_thread(_refresh)
