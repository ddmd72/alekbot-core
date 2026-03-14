"""
Port contract tests for JobRunnerPort.

Covers:
- ABC structure + abstract method enforcement
- run_job signature contract
- AsyncMock(spec=JobRunnerPort) usability
"""
import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock

from src.ports.job_runner_port import JobRunnerPort


class TestJobRunnerPortContract:

    def test_is_abstract_class(self):
        assert issubclass(JobRunnerPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            JobRunnerPort()

    def test_has_run_job_abstract_method(self):
        assert getattr(JobRunnerPort.run_job, "__isabstractmethod__", False)

    def test_run_job_is_async(self):
        assert inspect.iscoroutinefunction(JobRunnerPort.run_job)

    def test_exactly_one_abstract_method(self):
        abstract = {
            name for name, method in inspect.getmembers(JobRunnerPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert abstract == {"run_job"}, f"Expected only run_job, got: {abstract}"

    def test_run_job_signature(self):
        sig = inspect.signature(JobRunnerPort.run_job)
        params = list(sig.parameters.keys())
        assert params == ["self", "job_name", "env_overrides"]

    def test_concrete_subclass_requires_run_job(self):
        class Incomplete(JobRunnerPort):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_subclass_with_run_job_instantiates(self):
        class Complete(JobRunnerPort):
            async def run_job(self, job_name, env_overrides):
                return "op-name"

        instance = Complete()
        assert isinstance(instance, JobRunnerPort)


class TestJobRunnerPortMockUsability:
    """AsyncMock(spec=JobRunnerPort) must satisfy the port contract."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=JobRunnerPort)

    async def test_run_job_returns_string(self, mock_port):
        mock_port.run_job.return_value = "operations/run-abc-123"
        result = await mock_port.run_job(
            job_name="alek-research-job-dev",
            env_overrides={"JOB_QUERY": "test"},
        )
        assert result == "operations/run-abc-123"

    async def test_run_job_called_with_correct_args(self, mock_port):
        mock_port.run_job.return_value = "op-name"
        await mock_port.run_job(
            job_name="alek-research-job-dev",
            env_overrides={"KEY": "VALUE"},
        )
        mock_port.run_job.assert_called_once_with(
            job_name="alek-research-job-dev",
            env_overrides={"KEY": "VALUE"},
        )

    async def test_run_job_raises_on_api_error(self, mock_port):
        mock_port.run_job.side_effect = RuntimeError("API error")
        with pytest.raises(RuntimeError):
            await mock_port.run_job(job_name="job", env_overrides={})

    async def test_run_job_empty_env_overrides(self, mock_port):
        mock_port.run_job.return_value = "op"
        result = await mock_port.run_job(job_name="job", env_overrides={})
        assert isinstance(result, str)
