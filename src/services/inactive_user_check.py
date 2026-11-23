"""InactiveUserCheck — whether an entry point must refuse a user (CABINET_DEVICE_SESSIONS_RFC §7.8 "Entry points").

A user is refused when the stored profile exists and `is_active` is false: the account lock (the duress code, or the
fifth wrong facts PIN in a row) or a user deactivated by hand. Every call reads the stored user fresh, through the
user repository, never a cached profile (the agent factory's cache would keep a locked user served until it expires).

A user that is not stored is not refused here: each entry point already handles an unknown user its own way (a task
for it fails or finds nothing, a ticket names no profile). A read error propagates, so the caller fails closed: a
`/worker` delivery answers 500 and Cloud Tasks retries it, a voice route answers 500, delivery sends nothing.

The holders that receive it (the `/worker` routes of both services, the voice control plane, the drive connector's
tool calls and channel resolution) take it as an optional collaborator: without one they do not check, so the
entry-point test (`tests/unit/test_locked_user_entry_points.py`) asserts that production wires each of them.
"""
from ..ports.user_repository import UserRepository


class InactiveUserCheck:

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def is_inactive(self, user_id: str) -> bool:
        """True when the stored user `user_id` exists and is inactive. A read error propagates."""
        user = await self._users.get_user(user_id)
        return user is not None and not user.is_active
