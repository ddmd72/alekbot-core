"""
Port contract tests for PuppeteerRunnerPort.

Covers:
- ABC structure + abstract method enforcement
- run signature contract
- PuppeteerRunnerError public exception
- AsyncMock(spec=PuppeteerRunnerPort) usability
"""
import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock

from src.ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort


class TestPuppeteerRunnerPortContract:

    def test_is_abstract_class(self):
        assert issubclass(PuppeteerRunnerPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PuppeteerRunnerPort()

    def test_has_run_abstract_method(self):
        assert getattr(PuppeteerRunnerPort.run, "__isabstractmethod__", False)

    def test_run_is_async(self):
        assert inspect.iscoroutinefunction(PuppeteerRunnerPort.run)

    def test_exactly_one_abstract_method(self):
        abstract = {
            name for name, method in inspect.getmembers(PuppeteerRunnerPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert abstract == {"run"}, f"Expected only run, got: {abstract}"

    def test_run_signature(self):
        sig = inspect.signature(PuppeteerRunnerPort.run)
        params = list(sig.parameters.keys())
        assert params == ["self", "html_code", "timeout"]

    def test_concrete_subclass_requires_run(self):
        class Incomplete(PuppeteerRunnerPort):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_subclass_with_run_instantiates(self):
        class Complete(PuppeteerRunnerPort):
            async def run(self, html_code, timeout):
                return b"%PDF-1.4"

        instance = Complete()
        assert isinstance(instance, PuppeteerRunnerPort)


class TestPuppeteerRunnerError:

    def test_is_exception_subclass(self):
        assert issubclass(PuppeteerRunnerError, Exception)

    def test_can_raise_and_catch(self):
        with pytest.raises(PuppeteerRunnerError, match="rendering failed"):
            raise PuppeteerRunnerError("rendering failed")

    def test_message_preserved(self):
        err = PuppeteerRunnerError("exit code 1\nstack trace")
        assert "exit code 1" in str(err)


class TestPuppeteerRunnerPortMockUsability:
    """AsyncMock(spec=PuppeteerRunnerPort) must satisfy the port contract."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=PuppeteerRunnerPort)

    async def test_run_returns_bytes(self, mock_port):
        mock_port.run.return_value = b"%PDF-1.4 fake-pdf"
        result = await mock_port.run(html_code="<html></html>", timeout=60)
        assert result == b"%PDF-1.4 fake-pdf"

    async def test_run_called_with_correct_args(self, mock_port):
        mock_port.run.return_value = b"pdf"
        await mock_port.run(html_code="<html>test</html>", timeout=30)
        mock_port.run.assert_called_once_with(
            html_code="<html>test</html>",
            timeout=30,
        )

    async def test_run_raises_puppeteer_error(self, mock_port):
        mock_port.run.side_effect = PuppeteerRunnerError("Puppeteer crash")
        with pytest.raises(PuppeteerRunnerError, match="Puppeteer crash"):
            await mock_port.run(html_code="<html></html>", timeout=60)

    async def test_run_raises_on_generic_error(self, mock_port):
        mock_port.run.side_effect = RuntimeError("unexpected error")
        with pytest.raises(RuntimeError):
            await mock_port.run(html_code="", timeout=60)
