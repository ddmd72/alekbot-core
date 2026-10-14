"""An outage the reader hit while reading a file carries the calls it made for that file (final review F1-1): the tick
charges an outage to the file only when the reader called the model for it, so a one-page window that always times
out, or a provider error the file always gets, reaches the tick with model_calls >= 1."""
import pytest

from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.exceptions import LLMTimeoutError, LLMUnavailableError

from .test_drive_reader_runner_halving import _answer_after, _request, _runner


@pytest.mark.parametrize("error", [LLMTimeoutError("t"), LLMUnavailableError("503")], ids=["timeout", "5xx"])
async def test_an_outage_on_the_files_window_carries_its_calls(tmp_path, error):
    # window 1-10 answers before 11-11 fails: an outage cancels the windows still in flight (§24.8)
    runner, _, _ = _runner(_answer_after("1-10", {"11-11": error}), pages_total=11)
    with pytest.raises(DriveModelUnavailable) as raised:
        await runner.extract(_request(tmp_path))
    assert raised.value.model_calls >= 2   # window 1 answered, the failing call made
    assert raised.value.pages_sent == 10
