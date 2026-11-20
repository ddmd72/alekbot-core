"""PuppeteerRunnerError, the public exception of the PuppeteerRunnerPort module."""
import pytest

from src.ports.puppeteer_runner_port import PuppeteerRunnerError


class TestPuppeteerRunnerError:
    def test_is_exception_subclass(self):
        assert issubclass(PuppeteerRunnerError, Exception)

    def test_can_raise_and_catch(self):
        with pytest.raises(PuppeteerRunnerError, match="rendering failed"):
            raise PuppeteerRunnerError("rendering failed")

    def test_message_preserved(self):
        err = PuppeteerRunnerError("exit code 1\nstack trace")
        assert "exit code 1" in str(err)
