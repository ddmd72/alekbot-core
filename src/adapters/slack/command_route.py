"""SlackCommandRoute — what Slack posts when someone types a slash command (`/geo look here`).

A slash command is the `$` command of the same name: the worker runs `$geo look here` in the bot's DM with the user. The
route only checks and queues; Slack allows 3 s, any `$` command may outlast that, and Cloud Run throttles the CPU after
the response, so the work is a queued Slack-events task. The order is the security: raw capped bytes, signature, and only
then the form is parsed. The answer is HTTP 200 with an EMPTY body (a JSON body would be shown as a message). Slack never
retries a slash command, so the task has no name."""
import time
from typing import Callable, Dict, Mapping, Tuple
from urllib.parse import parse_qs

from ...ports.task_queue import TaskQueue
from ...utils.logger import logger
from ...utils.telemetry import get_trace_ids, inject_trace_headers
from .signature import verify_slack_signature

SLASH_COMMAND_EVENT = "slash_command"


class SlackCommandRoute:
    """`handle(body, headers)` -> `(text body, status)`: 401 for a bad signature (nothing parsed), 400 for a signed body
    without a `/command` or a `user_id`, 500 when the queue refused, otherwise 200 with an empty body."""

    def __init__(self, signing_secret: str, queue: TaskQueue, clock: Callable[[], float] = time.time) -> None:
        self._secret = signing_secret
        self._queue = queue
        self._clock = clock

    async def handle(self, body: bytes, headers: Mapping[str, str]) -> Tuple[str, int]:
        if not verify_slack_signature(body, headers, self._secret, self._clock()):
            logger.warning("⚠️ Invalid Slack signature on a slash command")
            return "", 401
        form = self._form(body)
        command = form.get("command", "")
        user_id = form.get("user_id", "")
        if not command.startswith("/") or not user_id:
            logger.warning("⚠️ Signed slash command without a command or a user")
            return "", 400
        trace_headers: Dict[str, str] = {}
        inject_trace_headers(trace_headers)
        trace_id = get_trace_ids().get("trace_id")
        if trace_id:
            trace_headers["x-trace-id"] = trace_id
        event = {"type": SLASH_COMMAND_EVENT, "command": command, "text": form.get("text", ""),
                 "user": user_id, "channel": form.get("channel_id", "")}
        try:
            await self._queue.enqueue_slack_event(
                event_data={"event": event}, session_id=user_id, trace_headers=trace_headers, task_name=None,
            )
        except Exception as exc:
            logger.error(f"❌ Slash command not queued: {type(exc).__name__}: {exc}", exc_info=True)
            return "", 500
        logger.info(f"📬 Slash command {command} enqueued")
        return "", 200

    @staticmethod
    def _form(body: bytes) -> Dict[str, str]:
        """The first value of every field of a signed form body; an undecodable body is an empty form."""
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except ValueError:
            return {}
        return {key: values[0] for key, values in parsed.items() if values}
