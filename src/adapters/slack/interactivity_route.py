"""SlackInteractivityRoute — what Slack posts when someone presses a button on a message the bot posted (Facts S4 plan
§3.20 "Inbound").

The order is the security: the signature is checked on the raw bytes first, and only a signed body is parsed. A signed
`block_actions` payload whose block carries a binding becomes an `InteractiveChoice` and is queued for the worker as an
`interactive_choice` task; nothing else is done here, and the answer is sent at once (Slack allows 3 s, and Cloud Run
throttles the CPU after the response, so no work follows it). What the choice means is the worker's."""
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs

from pydantic import ValidationError

from ...domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from ...domain.messaging import MessageRef
from ...ports.task_queue import TaskQueue
from ...utils.logger import logger
from .binding_codec import decode
from .signature import verify_slack_signature

PLATFORM = "slack"
_OK: Dict[str, Any] = {"ok": True}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


class SlackInteractivityRoute:
    """`handle(body, headers)` -> `(json body, status)`: 401 for an unsigned or badly signed request (nothing parsed), 400
    for a signed body that is not a form with a JSON `payload`, 503 when there is no queue, 500 when the queue refused the
    task, otherwise 200 (also for everything that is not a button press of ours, which is ignored)."""

    def __init__(
        self, signing_secret: str, queue: Optional[TaskQueue], clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = signing_secret
        self._queue = queue
        self._clock = clock

    async def handle(self, body: bytes, headers: Mapping[str, str]) -> Tuple[Dict[str, Any], int]:
        if not verify_slack_signature(body, headers, self._secret, self._clock()):
            return {"error": "Invalid signature"}, 401
        payload = self._payload(body)
        if payload is None:
            return {"error": "Invalid payload"}, 400
        if payload.get("type") != "block_actions":
            logger.info(f"[SlackInteractivity] a {_text(payload.get('type')) or 'typeless'} payload; ignored")
            return dict(_OK), 200
        choice, dedup_key = self._choice(payload)
        if choice is None:
            return dict(_OK), 200
        if self._queue is None:
            logger.error("[SlackInteractivity] no task queue; the choice cannot be taken")
            return {"error": "interactivity not configured"}, 503
        try:
            await self._queue.enqueue_worker_task(INTERACTIVE_CHOICE_TASK, choice.to_payload(), dedup_key=dedup_key)
        except Exception as exc:
            logger.error(f"[SlackInteractivity] the choice was not queued: {type(exc).__name__}: {exc}", exc_info=True)
            return {"error": "not queued"}, 500
        logger.info(f"[SlackInteractivity] queued a {choice.binding.kind} choice")
        return dict(_OK), 200

    @staticmethod
    def _payload(body: bytes) -> Optional[Dict[str, Any]]:
        """The JSON object in the form field `payload` of a signed body, or None."""
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True).get("payload")
            parsed = json.loads(values[0]) if values else None
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _choice(self, payload: Mapping[str, Any]) -> Tuple[Optional[InteractiveChoice], Optional[str]]:
        """The choice a press stands for and the dedup key of the press, or `(None, None)` for anything that is not a press
        of a button of ours: a block without our binding, no user, no message."""
        user_id = _text(_section(payload, "user").get("id"))
        container = _section(payload, "container")
        channel_id = _text(_section(payload, "channel").get("id")) or _text(container.get("channel_id"))
        message_id = _text(_section(payload, "message").get("ts")) or _text(container.get("message_ts"))
        actions = payload.get("actions")
        for action in actions if isinstance(actions, list) else []:
            if not isinstance(action, dict):
                continue
            block_id = _text(action.get("block_id"))
            binding = decode(block_id)
            if binding is None:
                continue
            action_ts = _text(action.get("action_ts"))
            if not (channel_id and message_id):
                logger.warning("[SlackInteractivity] a press of our block without its message; ignored")
                return None, None
            try:
                choice = InteractiveChoice(
                    binding=binding, option_id=_text(action.get("action_id")), platform=PLATFORM,
                    platform_user_id=user_id, message_ref=MessageRef(PLATFORM, channel_id, message_id),
                    at=self._moment(action_ts),
                )
            except ValidationError:
                logger.warning("[SlackInteractivity] a press of our block without a user or an option; ignored")
                return None, None
            return choice, f"{action_ts}:{user_id}:{block_id}" if action_ts else None
        logger.info("[SlackInteractivity] no action of ours in the payload; ignored")
        return None, None

    def _moment(self, action_ts: str) -> datetime:
        """When the button was pressed: the action's own timestamp, else the time the request came."""
        try:
            return datetime.fromtimestamp(float(action_ts), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return datetime.fromtimestamp(self._clock(), tz=timezone.utc)
