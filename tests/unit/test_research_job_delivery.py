"""
Unit tests for job_main delivery + billing wiring.

Regression cover for the 2026-06-06 incident: the deep-research Cloud Run Job
enqueued its HtmlPageGenerator delivery task with NO OIDC token
(service_account_email=None), so the /worker OIDC gate answered 401 and the
(expensive) research result was never delivered.

Two invariants are locked here:
  1. _build_task_queue() attaches the OIDC identity from SERVICE_ACCOUNT_EMAIL so
     the delivery task passes the /worker gate. Symmetric with main.py's enqueue
     side and src/web/worker_oidc_verifier.py's verify side.
  2. _record_billing() records the DR token cost (incl. cache tokens) and is
     decoupled from delivery — the cost is incurred at research time, so a failed
     delivery must not skip billing.

Mock boundary: GcpTaskQueue / FirestoreAccountRepository constructors are patched
in the job_main namespace (no real Cloud Tasks / Firestore clients created).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import job_main


# ---------------------------------------------------------------------------
# _build_task_queue — OIDC identity attachment (the incident)
# ---------------------------------------------------------------------------

class TestBuildTaskQueueOidc:

    def _env(self, monkeypatch, sa):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("CLOUD_RUN_SERVICE_URL", "https://svc.example")
        monkeypatch.setenv("APP_ENV", "development")
        if sa is None:
            monkeypatch.delenv("SERVICE_ACCOUNT_EMAIL", raising=False)
        else:
            monkeypatch.setenv("SERVICE_ACCOUNT_EMAIL", sa)

    def test_attaches_oidc_when_sa_set(self, monkeypatch):
        # The bug: this kwarg was hard-coded to None → task had no OIDC → /worker 401.
        self._env(monkeypatch, "worker-sa@proj.iam.gserviceaccount.com")
        with patch.object(job_main, "GcpTaskQueue") as MockQ:
            job_main._build_task_queue()
        kwargs = MockQ.call_args.kwargs
        assert kwargs["service_account_email"] == "worker-sa@proj.iam.gserviceaccount.com"

    def test_no_oidc_when_sa_unset(self, monkeypatch):
        # Local-dev path: /worker gate bypasses when SERVICE_ACCOUNT_EMAIL is unset,
        # so a None identity is correct there (and must not crash).
        self._env(monkeypatch, None)
        with patch.object(job_main, "GcpTaskQueue") as MockQ:
            job_main._build_task_queue()
        assert MockQ.call_args.kwargs["service_account_email"] is None

    def test_targets_env_prefixed_queue(self, monkeypatch):
        self._env(monkeypatch, "sa@proj.iam.gserviceaccount.com")
        with patch.object(job_main, "GcpTaskQueue") as MockQ:
            job_main._build_task_queue()
        assert MockQ.call_args.kwargs["queue_name"] == "agent-tasks-dev"


# ---------------------------------------------------------------------------
# _record_billing — DR usage reaches Firestore, incl. cache tokens
# ---------------------------------------------------------------------------

class TestRecordBilling:

    def _patch_repo(self):
        repo = MagicMock()
        repo.increment_account_usage = AsyncMock()
        return repo

    async def test_records_tokens_and_cache_cost(self):
        repo = self._patch_repo()
        result = {
            "total_tokens": 100_000,
            "cache_read_tokens": 40_000,
            "cache_write_tokens": 5_000,
        }
        with patch.object(job_main, "_build_account_repo", return_value=repo):
            await job_main._record_billing("acct-1", "claude-sonnet-4-6", result)

        repo.increment_account_usage.assert_awaited_once()
        kw = repo.increment_account_usage.await_args.kwargs
        assert kw["account_id"] == "acct-1"
        assert kw["tokens"] == 100_000
        # claude-sonnet-4-6 input $3/M; prompt 100k = $0.30, +cache_read 40k*0.1*3/M
        # +cache_write 5k*1.25*3/M → strictly greater than the no-cache cost.
        assert kw["cost"] > (100_000 / 1_000_000) * 3.0

    async def test_noop_when_no_tokens(self):
        repo = self._patch_repo()
        with patch.object(job_main, "_build_account_repo", return_value=repo):
            await job_main._record_billing(
                "acct-1", "claude-sonnet-4-6",
                {"total_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
            )
        repo.increment_account_usage.assert_not_awaited()

    async def test_noop_when_no_account(self):
        repo = self._patch_repo()
        with patch.object(job_main, "_build_account_repo", return_value=repo):
            await job_main._record_billing("", "claude-sonnet-4-6", {"total_tokens": 9_999})
        repo.increment_account_usage.assert_not_awaited()

    async def test_swallows_repo_failure(self):
        # Billing is best-effort: a Firestore error must not crash the job (delivery
        # has its own exit path).
        repo = self._patch_repo()
        repo.increment_account_usage.side_effect = RuntimeError("firestore down")
        with patch.object(job_main, "_build_account_repo", return_value=repo):
            await job_main._record_billing("acct-1", "claude-sonnet-4-6", {"total_tokens": 9_999})
        # no exception propagated
