"""`CallTextFileService`: the call's text kept as a file in the drive Inbox or the chat. Real `UserDriveService` over
`FakeDriveAdapter`, a real `UserNotificationService` over a recording media port, a scripted extractor."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.call_text_file import CallTextEntry, CallTextRequest
from src.domain.drive_connection import DriveConnection
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.language import LanguageCode
from src.domain.notification import NotificationChannel
from src.domain.ui_messages import StatusType, UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.domain.voice_mode import VoiceMode
from src.ports.companion_extractor_port import CompanionExtractorPort
from src.ports.language_service_port import LanguageServicePort
from src.ports.localization_port import LocalizationPort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.ports.prompt_content_store import PromptContentStore
from src.ports.session_store import SessionStore
from src.ports.user_repository import UserRepository
from src.services.call_text_file_service import CallTextFileService
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from src.services.user_notification_service import UserNotificationService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

NOW = datetime(2025, 8, 10, 12, 10, tzinfo=timezone.utc)   # 14:10 in Rome; the call lasted 600 s
ENTRIES = (CallTextEntry("We have a booking under RM-55120.", "Abbiamo una prenotazione con il codice RM-55120."),)
NAME = "2025-08-10 14.00 Check-in at Albergo Immaginario.md"


class ScriptedExtractor(CompanionExtractorPort):
    def __init__(self, result=None, error=None, delay=0.0):
        self.result = result if result is not None else {"records": [], "title": "Check-in at Albergo Immaginario",
                                                         "summary": "Alex Doe checked in at Albergo Immaginario with booking RM-55120."}
        self.error, self.delay, self.calls = error, delay, []

    async def extract(self, companion_type: str, account_id: str, created_by_user_id: str,
                      messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.calls.append((companion_type, account_id, created_by_user_id, messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


FOLDER_NAMES = {LanguageCode.EN: "Voice calls", LanguageCode.UK: "Голосові дзвінки"}


class FakeLocalization(LocalizationPort):
    """Only the call-files folder name is real; the rest of the port is unused by the service."""

    def get_status_phrases(self, lang: LanguageCode, status: StatusType) -> List[str]:
        return []

    def get_entertainment_intros(self, lang: LanguageCode) -> List[str]:
        return []

    def get_file_prompt(self, lang: LanguageCode, mime_type: str) -> str:
        return ""

    def get_ui_string(self, lang: LanguageCode, message: UIMessage) -> str:
        assert message is UIMessage.CALL_FILES_DRIVE_FOLDER
        return FOLDER_NAMES[lang]

    def get_ui_string_variants(self, message: UIMessage) -> List[str]:
        return list(FOLDER_NAMES.values())


class FakeLanguage(LanguageServicePort):
    def __init__(self, lang: LanguageCode = LanguageCode.EN, error: Exception | None = None):
        self.lang, self.error, self.calls = lang, error, []

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.lang

    async def get_preference(self, user_id: str):
        return None, False


class World:
    def __init__(self, primary=True, writable=True, chat=True, tz="Europe/Rome", extractor=None, **kw):
        self.fake = FakeDriveAdapter("fake_b", writable=writable)
        self.repo = FakeDriveConnectionRepository()
        self.conn = DriveConnection(connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="c2",
                                    kind="fake_b", drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme",
                                    created_at=NOW)
        self.repo.connections[("usr-1", "conn-2")] = self.conn
        if primary:
            self.repo.settings["usr-1"] = DriveUserSettings(user_id="usr-1", primary_connection_id="conn-2")
        root = self.fake.tree(self.conn.handle).root.item_id
        self.rd = self.fake.plant(self.conn.handle, root, "R&D", folder=True)
        self.repo.connections[("usr-1", "conn-2")] = replace(self.conn, write_folder_id=self.rd)
        self.drive = UserDriveService(self.fake, DriveConnectionService(self.repo, None))

        state = AsyncMock(spec=NotificationStatePort)
        state.get_primary.return_value = None
        state.get.return_value = NotificationChannel(user_id="usr-1", platform="telegram", channel_id="12345",
                                                     updated_at=datetime(2026, 1, 1))
        factory = MagicMock(spec=NotificationChannelFactoryPort)
        self.media = AsyncMock()
        self.media.upload_file = AsyncMock()
        self.sessions = AsyncMock(spec=SessionStore)
        self.notifications = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=MagicMock(),
                                                     notification_sla={}, session_store=self.sessions)
        if chat:
            self.notifications.register_platform_media("telegram", self.media)

        self.users = AsyncMock(spec=UserRepository)
        self.users.get_user.return_value = UserProfile(user_id="usr-1", account_id="acc-1",
                                                       config=UserBotConfig(timezone=tz))
        self.extractor = extractor or ScriptedExtractor()
        self.prompt_content = AsyncMock(spec=PromptContentStore)
        self.language = FakeLanguage()
        self.service = CallTextFileService(self.extractor, self.drive, self.notifications, self.users,
                                           localization=FakeLocalization(), language=self.language,
                                           prompt_content=self.prompt_content, now=lambda: NOW, **kw)

    def request(self, target="drive", entries=ENTRIES):
        return CallTextRequest(target=target, mode=VoiceMode.INTERPRETER, duration_s=600, entries=entries)

    def files(self):
        tree = self.fake.tree(self.conn.handle)
        return {tree.path(n): n.data for n in tree.nodes.values() if not n.is_folder}


async def test_the_drive_file_lands_in_the_inbox_with_the_rendered_markdown():
    w = World()
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path == f"Acme/R&D/Inbox/Voice calls/{NAME}"
    data = w.files()[f"R&D/Inbox/Voice calls/{NAME}"].decode("utf-8")
    assert data.startswith("# Check-in at Albergo Immaginario\n\n2025-08-10 14:00–14:10\n\nAlex Doe checked in at Albergo Immaginario with booking RM-55120.")
    assert "> We have a booking under RM-55120.\n\nAbbiamo una prenotazione con il codice RM-55120.\n" in data


@pytest.mark.parametrize("mode, summarizer_type", [(VoiceMode.INTERPRETER, "voice_interpreter"),
                                                    (VoiceMode.TUTOR, "voice_tutor")])
async def test_the_summarizer_is_asked_with_the_modes_type_and_the_calls_turns(mode, summarizer_type):
    # Each mode reads its own summarizer prompt (VOICE_CALL_LIVE_TEXT_RFC §4.11 "Title and summary"), never the
    # companion's "voice".
    w = World()
    await w.service.save("usr-1", "acc-1", replace(w.request(), mode=mode))
    assert w.extractor.calls == [(summarizer_type, "acc-1", "usr-1",
                                  [{"kind": "exchange", "request_text": "We have a booking under RM-55120.",
                                    "response_text": "Abbiamo una prenotazione con il codice RM-55120.", "interrupted": False}])]


async def test_the_chat_gets_one_file_and_the_history_is_unchanged():
    w = World()
    out = await w.service.save("usr-1", "acc-1", w.request("chat"))
    assert out.saved and out.path == ""
    w.media.upload_file.assert_awaited_once()
    kwargs = w.media.upload_file.await_args.kwargs
    assert kwargs["filename"] == NAME and kwargs["title"] == "Check-in at Albergo Immaginario" and kwargs["channel_id"] == "12345"
    assert kwargs["file_bytes"].decode("utf-8").startswith("# Check-in at Albergo Immaginario")
    assert w.sessions.mock_calls == []
    assert w.files() == {}


async def test_a_chat_with_no_media_port_is_no_chat():
    w = World(chat=False)
    out = await w.service.save("usr-1", "acc-1", w.request("chat"))
    assert (out.saved, out.reason) == (False, "no_chat")


async def test_no_primary_is_not_connected():
    out = await (w := World(primary=False)).service.save("usr-1", "acc-1", w.request())
    assert (out.saved, out.reason) == (False, "not_connected")


async def test_no_drive_on_the_deployment_is_not_connected():
    w = World()
    service = CallTextFileService(w.extractor, None, w.notifications, w.users, localization=FakeLocalization(),
                               language=w.language, now=lambda: NOW)
    out = await service.save("usr-1", "acc-1", w.request())
    assert (out.saved, out.reason) == (False, "not_connected")


async def test_a_primary_without_a_write_folder_is_refused():
    w = World()
    w.repo.connections[("usr-1", "conn-2")] = replace(w.conn, write_folder_id=None)
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert (out.saved, out.reason) == (False, "refused")
    assert w.files() == {}


async def test_an_unexpected_drive_error_is_failed():
    w = World()
    w.drive.save_to_primary = AsyncMock(side_effect=RuntimeError("boom"))
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert (out.saved, out.reason) == (False, "failed")


async def test_a_summarizer_error_still_saves_with_the_fallback_title():
    w = World(extractor=ScriptedExtractor(error=RuntimeError("llm down")))
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path == "Acme/R&D/Inbox/Voice calls/2025-08-10 14.00 Call.md"
    assert w.files()["R&D/Inbox/Voice calls/2025-08-10 14.00 Call.md"].decode("utf-8").startswith("# Call\n\n2025-08-10 14:00–14:10\n\n---")


async def test_a_summarizer_past_its_budget_still_saves_without_a_summary():
    w = World(extractor=ScriptedExtractor(delay=5), summary_timeout_s=0.05)
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path.endswith("2025-08-10 14.00 Call.md")


async def test_a_malformed_summary_saves_with_the_fallback():
    w = World(extractor=ScriptedExtractor(result={"records": [], "title": 3, "summary": None}))
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path.endswith("Call.md")


async def test_the_prompt_content_store_is_flushed_on_success_and_on_failure():
    w = World()
    await w.service.save("usr-1", "acc-1", w.request())
    w.prompt_content.flush.assert_awaited_once()
    w = World(extractor=ScriptedExtractor(error=RuntimeError("x")))
    await w.service.save("usr-1", "acc-1", w.request())
    w.prompt_content.flush.assert_awaited_once()


async def test_an_unreadable_user_means_utc():
    w = World()
    w.users.get_user.side_effect = RuntimeError("down")
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path.endswith("2025-08-10 12.00 Check-in at Albergo Immaginario.md")


async def test_the_name_follows_the_users_timezone():
    w = World(tz="America/New_York")
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.path.endswith("2025-08-10 08.00 Check-in at Albergo Immaginario.md")


async def test_save_never_raises():
    w = World()
    w.notifications.notify_file_bytes = AsyncMock(side_effect=RuntimeError("boom"))
    out = await w.service.save("usr-1", "acc-1", w.request("chat"))
    assert (out.saved, out.reason) == (False, "failed")


async def test_drive_available_is_true_for_a_writable_primary():
    assert await World().service.drive_available("usr-1") is True


async def test_drive_available_is_false_without_a_primary_or_a_drive():
    w = World(primary=False)
    assert await w.service.drive_available("usr-1") is False
    nodrive = CallTextFileService(w.extractor, None, w.notifications, w.users, localization=FakeLocalization(),
                               language=w.language)
    assert await nodrive.drive_available("usr-1") is False


async def test_drive_available_is_false_on_an_error():
    w = World()
    w.drive.can_save_to_primary = AsyncMock(side_effect=RuntimeError("boom"))
    assert await w.service.drive_available("usr-1") is False


async def test_the_first_drive_save_creates_the_voice_calls_folder_and_files_into_it():
    w = World()
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path == f"Acme/R&D/Inbox/Voice calls/{NAME}"
    assert list(w.files()) == [f"R&D/Inbox/Voice calls/{NAME}"]


async def test_a_second_drive_save_reuses_the_folder():
    w = World()
    await w.service.save("usr-1", "acc-1", w.request())
    out = await w.service.save("usr-1", "acc-1", replace(w.request(), duration_s=300))   # another start, another name
    assert out.saved
    tree = w.fake.tree(w.conn.handle)
    folders = sorted(tree.path(n) for n in tree.nodes.values() if n.is_folder)
    assert [f for f in folders if "Voice calls" in f] == ["R&D/Inbox/Voice calls"]
    assert len(w.files()) == 2
    assert all(path.startswith("R&D/Inbox/Voice calls/") for path in w.files())


async def test_the_folder_name_follows_the_owners_ui_language():
    w = World()
    await w.service.save("usr-1", "acc-1", w.request())
    w.language.lang = LanguageCode.UK
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert out.saved and out.path == f"Acme/R&D/Inbox/Голосові дзвінки/{NAME}"
    assert sorted(p.rsplit("/", 1)[0] for p in w.files()) == ["R&D/Inbox/Voice calls", "R&D/Inbox/Голосові дзвінки"]


async def test_an_unresolvable_ui_language_fails_and_writes_nothing():
    w = World()
    w.language.error = RuntimeError("down")
    out = await w.service.save("usr-1", "acc-1", w.request())
    assert (out.saved, out.reason) == (False, "failed")
    assert w.files() == {}


async def test_the_chat_target_writes_nothing_to_the_drive():
    w = World()
    out = await w.service.save("usr-1", "acc-1", w.request("chat"))
    assert out.saved and w.files() == {}
