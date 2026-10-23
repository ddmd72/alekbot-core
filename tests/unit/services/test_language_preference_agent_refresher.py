"""`attach_agent_refresher` gives LanguagePreferenceService the agent factory's per-user refresh.

The refresher it is given is the one `set_preference` calls, once per language change and before the notification that
tells the model about the change. Without one the notification still goes out.
"""
from typing import List

from src.domain.billing import BillingAccount
from src.domain.language import LanguageCode
from src.domain.prompt_v3.slot import OwnerType
from src.domain.user import UserProfile
from src.services.language_preference_service import LanguagePreferenceService

USER_ID = "u1"
ACCOUNT_ID = "a1"


class _Users:
    def __init__(self) -> None:
        self.user = UserProfile(user_id=USER_ID, account_id=ACCOUNT_ID)

    async def get_user(self, user_id: str):
        return self.user if user_id == USER_ID else None

    async def update_user(self, user: UserProfile):
        self.user = user
        return user


class _Accounts:
    async def get_account(self, account_id: str):
        return BillingAccount(account_id=account_id)


class _Profiles:
    async def set_override_tokens(self, owner_type: OwnerType, owner_id: str, tokens, clear_ids=frozenset()):
        return None


class _PromptBuilder:
    def invalidate_cache(self) -> None:
        pass


class _Notifier:
    def __init__(self, events: List[str]) -> None:
        self.events = events

    async def notify(self, **kwargs) -> None:
        self.events.append("notify")


def _service(events: List[str]) -> LanguagePreferenceService:
    return LanguagePreferenceService(
        user_repo=_Users(),
        account_repo=_Accounts(),
        profile_repo=_Profiles(),
        prompt_builder=_PromptBuilder(),
        system_default_language=LanguageCode.EN,
        notification_service=_Notifier(events),
    )


async def test_the_attached_refresher_runs_for_the_user_before_the_notification():
    events: List[str] = []
    service = _service(events)

    async def refresher(user_id: str) -> None:
        events.append(f"refresh:{user_id}")

    service.attach_agent_refresher(refresher)
    await service.set_preference(USER_ID, LanguageCode.UK, agent_mirror=False)

    assert events == [f"refresh:{USER_ID}", "notify"]


async def test_attaching_again_replaces_the_refresher():
    events: List[str] = []
    service = _service(events)

    async def first(user_id: str) -> None:
        events.append("first")

    async def second(user_id: str) -> None:
        events.append("second")

    service.attach_agent_refresher(first)
    service.attach_agent_refresher(second)
    await service.set_preference(USER_ID, LanguageCode.UK, agent_mirror=False)

    assert events == ["second", "notify"]


async def test_without_a_refresher_the_notification_still_goes_out():
    events: List[str] = []
    service = _service(events)

    await service.set_preference(USER_ID, LanguageCode.UK, agent_mirror=False)

    assert events == ["notify"]


async def test_a_failing_refresher_is_logged_and_the_change_is_kept():
    events: List[str] = []
    service = _service(events)

    async def broken(user_id: str) -> None:
        raise RuntimeError("factory down")

    service.attach_agent_refresher(broken)
    await service.set_preference(USER_ID, LanguageCode.UK, agent_mirror=False)

    assert events == []
    assert (await service.get_preference(USER_ID)) == (LanguageCode.UK, False)
