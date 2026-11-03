"""DocPlanner and the prefetch note: an unreadable file ends the task with the note; a note is never document text."""
import logging

from src.domain.agent import AgentIntent, AgentMessage
from tests.unit.agents.test_doc_planner_agent import (  # noqa: F401  (fixtures)
    agent, mock_coordinator, mock_llm, mock_prompt_builder,
)

NOTE = "[System: The file 'flat_viewing_uk.md' could not be opened. Do not guess what it contains.]"


def _message(**extra):
    return AgentMessage(intent=AgentIntent.DELEGATE, payload={"query": "Make a report", **extra},
                        sender="quick_response_agent", recipient="doc_planner_agent", task_id="task_1",
                        context={"user_id": "user123", "account_id": "acc1"})


async def test_a_note_without_content_fails_the_task_with_the_note(agent, mock_llm, caplog):
    with caplog.at_level(logging.DEBUG):
        resp = await agent.execute(_message(file_note=NOTE))
    assert resp.status.value == "failed" and NOTE in str(resp.error)
    mock_llm.generate_content.assert_not_called()
    assert any(r.levelno == logging.WARNING and "not readable" in r.getMessage() for r in caplog.records)


async def test_a_note_next_to_content_is_not_added_to_the_query(agent, mock_llm):
    await agent.execute(_message(file_note=NOTE, file_content="Viewing notes."))
    request = mock_llm.generate_content.call_args.kwargs.get("request") or \
        mock_llm.generate_content.call_args.args[0]
    sent = repr(request.messages)
    assert "Viewing notes." in sent and "could not be opened" not in sent
