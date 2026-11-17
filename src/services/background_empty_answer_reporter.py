"""The report for a background turn that ended with an empty answer: one ERROR line and one ops alert."""
from __future__ import annotations

from typing import Optional

from ..domain.notify_result import empty_answer_alert
from ..ports.alert_sink import AlertSinkPort
from ..utils.logger import logger


class BackgroundEmptyAnswerReporter:
    """Writes the final-not-retried report of an empty background answer for the `/worker` task runners."""

    def __init__(self, *, alert_sink: Optional[AlertSinkPort]) -> None:
        self._alert_sink = alert_sink

    async def report(self, path: str, user_id: str, note_id: Optional[str] = None) -> None:
        """One ERROR line and one ops alert (when a sink is configured), no content in either; a sink failure is logged."""
        logger.error(
            "[Worker] background_empty_answer path=%s note=%s user=%s",
            path, note_id, user_id[:8],
            extra={"event": "background_empty_answer", "path": path, "note_id": note_id,
                   "user_id": user_id[:8]},
        )
        if self._alert_sink is None:
            return
        try:
            await self._alert_sink.post(empty_answer_alert(path, user_id, note_id))
        except Exception as exc:
            logger.warning("[Worker] background_empty_answer alert failed: %s", exc)
