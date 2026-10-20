"""Smart's active_reminders line shows a reminder's workspace file (AGENT_WORKSPACE_RFC §8.3)."""
from tests.unit.services.test_prompt_assembly_service import TEMPLATE, _make_service


async def _render(notes):
    return await _make_service()._inject_runtime_context(
        prompt=TEMPLATE, biographical_facts=[], conversation_history=[], user_id="test_user", agent_notes=notes,
    )


async def test_the_line_shows_the_workspace_file():
    result = await _render([{"note_id": "1729401234567", "text": "Morning briefing",
                             "workspace_file": "projects/morning-briefing.md"}])
    assert ("    - Morning briefing, workspace file: workspace:projects/morning-briefing.md "
            "[id: 1729401234567]") in result


async def test_an_unset_field_adds_nothing():
    result = await _render([{"note_id": "1", "text": "Plain", "workspace_file": None}])
    assert "    - Plain [id: 1]" in result
