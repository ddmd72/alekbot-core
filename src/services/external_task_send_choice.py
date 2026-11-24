"""ExternalTaskSendChoice — a press of the Slack «Send» button on a held external task's command message: the same
release as the pasted `$task send <code>` (EXTERNAL_TASK_QUEUE_RFC §5.3, `decisions/external_task_confirmation_and_alerts.md`).

The button is the `external_task_send` binding of the shared interactive-message mechanism (Facts S4 plan §3.20): the
Slack route queues the press, `InteractiveChoiceService` resolves the clicker through IAM (an unknown or locked user
stops there, nothing done) and hands the choice here with the resolved user. The binding's id is the task, the option's
id the confirmation code (`external_task_receipt.send_button_message`).

1. Owner check: the task must be the clicker's (`ExternalTaskService.is_owner`). Another user's press (a shared
   channel) is None: nothing released, nothing shown on the owner's message.
2. The release is `confirm(user_id, code)`, the command's own call, and the outcome is the command's answer, shown in
   place of the button in the owner's UI language:
   - sent: `EXTERNAL_TASK_SENT`, the button gone;
   - refused: `send_refusal_reply`, the line the command gives. Not found (a second press, a task the command already
     sent, a lapsed hold) removes the button, since that code can never work again; unavailable (the flag off, the
     account locked since the press) keeps it, since the code still works once the queue is open again;
   - an unexpected error: logged, and `EXTERNAL_TASK_UNAVAILABLE` with the button kept, as the command answers it.
   `confirm` changes the task in one store transaction keyed on the hold, so no press, however repeated, releases a
   task twice.

A store error of the owner check or a failed language read raises before anything is written, so the worker task is
retried (`InteractiveChoiceService`'s rule).
"""
from typing import TYPE_CHECKING, Any, Optional

from ..domain.choice_outcome import ChoiceOutcome
from ..domain.external_task_receipt import EXTERNAL_TASK_SEND_KIND, send_button_message, send_refusal_reply
from ..domain.external_task_refusal import ExternalTaskRefused
from ..domain.interactive_choice import InteractiveChoice
from ..domain.ui_messages import UIMessage
from ..ports.choice_handler_port import ChoiceHandlerPort
from ..ports.language_service_port import LanguageServicePort
from ..ports.localization_port import LocalizationPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .external_task_service import ExternalTaskService


class ExternalTaskSendChoice(ChoiceHandlerPort):

    def __init__(
        self, tasks: "ExternalTaskService", language: LanguageServicePort, localization: LocalizationPort,
    ) -> None:
        self._tasks = tasks
        self._language = language
        self._localization = localization

    @property
    def kind(self) -> str:
        return EXTERNAL_TASK_SEND_KIND

    async def handle(self, choice: InteractiveChoice, user_id: str, account_id: str) -> Optional[ChoiceOutcome]:
        task_id, code = choice.binding.id, choice.option_id
        if not await self._tasks.is_owner(user_id, task_id):
            logger.info(f"[TaskQueue] send button user={user_id[:8]} task={task_id}: not the clicker's task; ignored")
            return None
        lang = await self._language.resolve_ui_language(user_id)

        def text(message: UIMessage, **fmt: Any) -> str:
            template = self._localization.get_ui_string(lang, message)
            return template.format(**fmt) if fmt else template

        message = send_button_message(task_id, code, text(UIMessage.EXTERNAL_TASK_SEND_BUTTON))
        try:
            sent = await self._tasks.confirm(user_id, code)
        except ExternalTaskRefused as e:
            logger.info(f"[TaskQueue] send button user={user_id[:8]} task={task_id}: refused {e.reason.value}")
            reply = send_refusal_reply(e.reason)
            return ChoiceOutcome(message=message, note=text(reply),
                                 keep_options=reply is UIMessage.EXTERNAL_TASK_UNAVAILABLE)
        except Exception as e:
            logger.error(f"[TaskQueue] send button user={user_id[:8]} task={task_id} failed: {type(e).__name__}",
                         exc_info=True)
            return ChoiceOutcome(message=message, note=text(UIMessage.EXTERNAL_TASK_UNAVAILABLE), keep_options=True)
        return ChoiceOutcome(message=message, note=text(UIMessage.EXTERNAL_TASK_SENT, title=sent.title,
                                                        worker=sent.label))
