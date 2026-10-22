"""Cross-cut review X8: the [DriveAgent] log line names the model that served the commission."""
import logging

from tests.support.scripted_llm import calls, script
from tests.unit.agents.test_drive_agent import REPORT, _agent, _msg, ops, prompts  # noqa: F401


async def test_x8_the_log_line_names_the_served_model(ops, prompts, caplog):
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script(calls(REPORT))
    with caplog.at_level(logging.INFO):
        await agent.execute(_msg())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveAgent] user="))
    assert "model=claude-sonnet-5-5" in line and "status=" in line
