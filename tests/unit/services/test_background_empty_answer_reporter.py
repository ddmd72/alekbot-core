"""BackgroundEmptyAnswerReporter: the one ERROR line and one ops alert of an empty background answer."""
import logging
from typing import List, Optional

import pytest

from src.domain.notify_result import empty_answer_alert
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter


class Sink:
    def __init__(self, fails: bool = False) -> None:
        self.posts: List[str] = []
        self._fails = fails

    async def post(self, text: str) -> None:
        if self._fails:
            raise RuntimeError("webhook down")
        self.posts.append(text)


def _records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]


@pytest.mark.parametrize("path,note_id", [
    ("execute_reminder", "note-1"),
    ("daily_email_review", None),
    ("deep_research_notice", None),
])
async def test_one_error_line_and_one_alert_with_the_ids_truncated(caplog, path: str, note_id: Optional[str]):
    sink = Sink()
    with caplog.at_level(logging.ERROR):
        await BackgroundEmptyAnswerReporter(alert_sink=sink).report(path, "user-12345678-long", note_id)  # type: ignore[arg-type]
    records = _records(caplog)
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert records[0].getMessage() == f"[Worker] background_empty_answer path={path} note={note_id} user=user-123"
    assert (records[0].path, records[0].note_id, records[0].user_id) == (path, note_id, "user-123")
    assert sink.posts == [empty_answer_alert(path, "user-12345678-long", note_id)]


async def test_without_a_sink_the_line_is_still_written(caplog):
    with caplog.at_level(logging.ERROR):
        await BackgroundEmptyAnswerReporter(alert_sink=None).report("daily_email_review", "user-1")
    assert len(_records(caplog)) == 1


async def test_a_failing_sink_is_logged_as_a_warning_and_never_raises(caplog):
    with caplog.at_level(logging.WARNING):
        await BackgroundEmptyAnswerReporter(alert_sink=Sink(fails=True)).report("execute_reminder", "user-1", "note-1")  # type: ignore[arg-type]
    assert len(_records(caplog)) == 1
    assert "[Worker] background_empty_answer alert failed: webhook down" in caplog.text
