"""The Slack «Send» button of a held external task, the whole way (EXTERNAL_TASK_QUEUE_RFC §5.3): the receipt's command
message posted by `ConversationHandler`, a signed press of its button at `POST /slack/interactivity`, the queued
`interactive_choice` task run by `WorkerHandler`, and the task sent, as `$task send` sends it.

Real objects throughout: the `HTTPModeAdapter` blueprint (registered as `main.py` registers it) with the real signing
check, the real `GcpTaskQueue` over a Cloud Tasks client that records its bodies, `WorkerHandler`,
`InteractiveChoiceService` over the real `IAMService` (users read fresh from the real `FirestoreUserRepository` on
`EvalDb`), `ExternalTaskSendChoice`, the real `ExternalTaskService` over the in-memory task stores, and the real
`InteractiveMessageRouter` / `SlackInteractiveMessageAdapter`, mocked at the slack_sdk client boundary only. The press
is built from the blocks the adapter actually posted. Alex Doe is `usr-1` (Slack `U0OWNER`); `usr-2` is another user of
the same bot in a shared channel (Slack `U0OTHER`)."""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.agent import DeliveryItem
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_jobs import EXTERNAL_TASK_TIMER
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from src.domain.interactive_choice import INTERACTIVE_CHOICE_TASK
from src.domain.language import LanguageCode
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.domain.user import InactiveReason
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from src.locales import en
from src.services.external_task_send_choice import ExternalTaskSendChoice
from src.services.iam_service import IAMService
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, USERS, ExternalTaskWorld, NOTE
from tests.support.fact_review_world import FakeLanguageService
from tests.support.task_receipt_file import receipt_parts
from tests.support.mcp_gate_fakes import FakeWhitelistRepo
from tests.unit.adapters.test_slack_interactivity_route import ACTION_TS, SECRET, form, sign
from tests.unit.web.gate_fakes import Tripwire

S = ExternalTaskStatus
CHANNEL, POSTED_TS = "D1", "1761295900.000200"


class Flow:
    def __init__(self, world: ExternalTaskWorld) -> None:
        self.world = world
        self.touched: List[str] = []
        self.slack = MagicMock()
        self.slack.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": CHANNEL, "ts": POSTED_TS})
        self.slack.chat_update = AsyncMock(return_value={"ok": True})
        router = InteractiveMessageRouter({"slack": SlackInteractiveMessageAdapter(self.slack)})
        language = FakeLanguageService()
        language.preferences[USER_ID] = (LanguageCode.EN, False)
        iam = IAMService(user_repo=world.users, account_repo=AsyncMock(),
                         whitelist_repo=FakeWhitelistRepo(emails=("alex@example.com", "other@example.com")))
        send = ExternalTaskSendChoice(world.service, language, FileLocalizationAdapter())
        self.chat = ConversationHandler(
            coordinator=MagicMock(), agent_factory=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30),
            commands=CommandServices(external_tasks=world.service, task_send_buttons=router),
        )
        self.worker = WorkerHandler(
            agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(), consolidation_service=None,
            agent_factory=MagicMock(), user_repo=MagicMock(),
            interactive_choice=InteractiveChoiceService(iam, {send.kind: send}, router),
        )
        adapter = HTTPModeAdapter(
            app=Tripwire("bolt_app", self.touched), config={"SLACK_SIGNING_SECRET": SECRET, "SLACK_BOT_TOKEN": "xoxb-test"},
            task_service=Tripwire("events_queue", self.touched), session_store=Tripwire("session_store", self.touched),
            conversation_handler=Tripwire("conversation_handler", self.touched), iam_service=Tripwire("iam", self.touched),
            worker_queue=world.queue,
        )
        app = Quart("slack_task_send")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")
        self.http = app.test_client()

    async def receipt(self):
        """A held task created in a chat turn, and its receipt delivered on Slack: the text file, the literal post, then
        the button."""
        created = await self.world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT, note=NOTE)
        channel = MagicMock()
        channel.platform, channel.channel_id, channel.language = "slack", CHANNEL, "en"
        channel.send_message, channel.send_literal, channel.send_file = AsyncMock(), AsyncMock(), AsyncMock()
        item = DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))
        await self.chat._deliver_item(item, channel, None, USER_ID)
        front, body = receipt_parts(channel.send_file.await_args.kwargs["content"])
        assert body == TEXT.encode("utf-8")
        assert front == {"title": TITLE, "worker": "research", "note": NOTE}
        channel.send_literal.assert_awaited_once()
        channel.send_message.assert_not_awaited()
        return created

    def press_payload(self, slack_user: str) -> Dict[str, Any]:
        """What Slack sends when `slack_user` presses the button of the message the adapter posted."""
        actions = self.slack.chat_postMessage.call_args.kwargs["blocks"][-1]
        [button] = actions["elements"]
        return {
            "type": "block_actions", "user": {"id": slack_user, "team_id": "T1"},
            "container": {"type": "message", "message_ts": POSTED_TS, "channel_id": CHANNEL},
            "channel": {"id": CHANNEL}, "message": {"ts": POSTED_TS},
            "actions": [{"type": "button", "block_id": actions["block_id"], "action_id": button["action_id"],
                         "value": button["value"], "action_ts": ACTION_TS}],
        }

    async def press(self, slack_user: str) -> Dict[str, Any]:
        """The signed press at the route, then the one queued choice run by the worker; the worker's answer."""
        body = form(self.press_payload(slack_user))
        response = await self.http.post("/slack/interactivity", data=body, headers=sign(body))
        assert response.status_code == 200 and await response.get_json() == {"ok": True}
        [queued] = self.world.jobs(INTERACTIVE_CHOICE_TASK)[-1:]
        answer, status = await self.worker.handle(queued)
        assert status == 200
        return answer


@pytest.fixture
async def flow():
    world = await ExternalTaskWorld().seed()
    for uid, slack_id in ((USER_ID, "U0OWNER"), ("usr-2", "U0OTHER")):
        await world.db.collection(USERS).document(uid).update({"platform_identities": {"slack": slack_id}})
    return Flow(world)


async def test_the_owners_press_is_queued_run_by_the_worker_and_sends_the_task_as_the_command_does(flow):
    created = await flow.receipt()

    answer = await flow.press("U0OWNER")

    assert answer == {"status": "shown"}
    assert flow.world.tasks.tasks[created.task_id].status is S.QUEUED
    assert [b["kind"] for b in flow.world.jobs(EXTERNAL_TASK_TIMER)] == ["hold", "deadline"]
    update = flow.slack.chat_update.call_args.kwargs
    assert (update["channel"], update["ts"]) == (CHANNEL, POSTED_TS)
    sent = en.UI_STRINGS[UIMessage.EXTERNAL_TASK_SENT.value].format(title=TITLE, worker="research")
    assert [b["type"] for b in update["blocks"]] == ["section", "context"]     # the button is gone
    assert update["blocks"][0]["text"]["text"] == f"`$task send {created.code}`"
    assert update["blocks"][1]["elements"][0]["text"] == sent
    assert flow.touched == []


async def test_a_second_press_gets_not_found_and_the_task_is_sent_once(flow):
    created = await flow.receipt()
    await flow.press("U0OWNER")
    sent_once = flow.world.tasks.tasks[created.task_id]
    second = {**flow.press_payload("U0OWNER")}
    second["actions"][0]["action_ts"] = "1761296001.000001"       # a new press, not Slack's retry of the first
    body = form(second)
    await flow.http.post("/slack/interactivity", data=body, headers=sign(body))
    answer, _ = await flow.worker.handle(flow.world.jobs(INTERACTIVE_CHOICE_TASK)[-1])

    assert answer == {"status": "shown"}
    assert flow.world.tasks.tasks[created.task_id] == sent_once
    assert flow.slack.chat_update.call_args.kwargs["blocks"][1]["elements"][0]["text"] == \
        en.UI_STRINGS[UIMessage.EXTERNAL_TASK_NOT_FOUND.value]


async def test_another_users_press_in_a_shared_channel_sends_nothing_and_leaves_the_message(flow):
    created = await flow.receipt()

    answer = await flow.press("U0OTHER")

    assert answer == {"status": "ignored"}
    assert flow.world.tasks.tasks[created.task_id].status is S.HELD
    flow.slack.chat_update.assert_not_called()


async def test_a_locked_owners_press_is_refused_at_iam_read_fresh_and_sends_nothing(flow):
    created = await flow.receipt()
    await flow.world.db.collection(USERS).document(USER_ID).update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})

    answer = await flow.press("U0OWNER")

    assert answer == {"status": "unknown_user"}
    assert flow.world.tasks.tasks[created.task_id].status is S.HELD
    flow.slack.chat_update.assert_not_called()
