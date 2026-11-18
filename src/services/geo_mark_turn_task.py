"""The `geo_mark_turn` worker task (GEO_MARKS_RFC §4.5): a `$geo` question queued with its mark.

It checks the payload `GeoMarkService.redeem_link` froze and hands it to the platform's turn runner (the Slack adapter's
entry, injected by composition); the runner re-authorizes the user and runs the chat turn with `geo_mark_note`. Without
a runner the deployment serves no geo turns and the task answers 501.
"""
from typing import Any, Awaitable, Callable, Optional, Tuple

from ..utils.logger import logger

PAYLOAD_KEYS = ("platform", "user_id", "account_id", "channel_id", "session_id", "platform_user_id", "text",
                "mark_text", "language", "link_fp")
TURN_PLATFORMS = ("slack",)

TurnRunner = Callable[[dict, dict], Awaitable[Tuple[dict, int]]]


class GeoMarkTurnTask:
    """A `WorkerTask`: 501 without a runner, 400 on a payload missing a key or for another platform, else the
    runner's answer to the payload's ten keys (the queue's `task_type` is not passed on)."""

    def __init__(self, *, run_turn: Optional[TurnRunner]) -> None:
        self._run_turn = run_turn

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        if self._run_turn is None:
            logger.warning("[Worker] geo_mark_turn: geo mark turns not configured")
            return {"error": "geo mark turns not configured"}, 501
        # The frozen payload of `GeoMarkService.redeem_link`, read key by key (a test matches producer and consumer keys).
        values = (payload.get("platform"), payload.get("user_id"), payload.get("account_id"),
                  payload.get("channel_id"), payload.get("session_id"), payload.get("platform_user_id"),
                  payload.get("text"), payload.get("mark_text"), payload.get("language"), payload.get("link_fp"))
        missing = [key for key, value in zip(PAYLOAD_KEYS, values) if not isinstance(value, str)]
        if missing:
            logger.error("[Worker] geo_mark_turn: payload without %s", ", ".join(missing))
            return {"error": "invalid geo_mark_turn payload"}, 400
        turn = dict(zip(PAYLOAD_KEYS, values))
        if turn["platform"] not in TURN_PLATFORMS:
            logger.error("[Worker] geo_mark_turn: unsupported platform %r", turn["platform"])
            return {"error": "unsupported platform"}, 400
        return await self._run_turn(turn, headers)
