"""An account-lock check that finds every user active (the constructor argument of services that require one)."""


class ActiveUsers:
    async def is_inactive(self, user_id: str) -> bool:
        return False
