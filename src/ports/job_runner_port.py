"""
JobRunnerPort — system boundary for triggering long-running background jobs.

Implemented by CloudRunJobsAdapter (Cloud Run Jobs v2 REST API).
Justification: system boundary (external GCP service), testable substitution.
"""
from abc import ABC, abstractmethod


class JobRunnerPort(ABC):
    """Port for triggering Cloud Run Job executions."""

    @abstractmethod
    async def run_job(
        self,
        job_name: str,
        env_overrides: dict[str, str],
    ) -> str:
        """
        Trigger a Cloud Run Job execution with per-run env var overrides.

        Fires and returns immediately — does NOT wait for the job to complete.

        Args:
            job_name:      Short job name (e.g. "alek-research-job-dev").
            env_overrides: Env vars injected into the job container for this run.

        Returns:
            Operation name returned by the Cloud Run Jobs API (usable as execution ID).

        Raises:
            Exception on API errors (non-2xx response, auth failure, etc.).
        """
