"""No consolidation prompt asks the model to write its reasoning out.

Anthropic's `reasoning_extraction` classifier declines a request whose prompt asks for the model's reasoning in the
output (a `<thinking>` section, worked examples in such tags, "show your work"); a declined batch is never consolidated.
The consolidator thinks natively (adaptive thinking), so its prompt never asks for written reasoning."""
import re

import pytest

from tests.support.prompt_render import render_static

PROFILES = ["consolidation", "consolidation_email", "consolidation_inbox", "consolidation_inbox_triage",
            "consolidation_review"]
ASKS_FOR_WRITTEN_REASONING = re.compile(
    r"</?(thinking|reasoning|scratchpad)>|show your work|reasoning tokens", re.IGNORECASE
)


@pytest.mark.parametrize("agent_id", PROFILES)
def test_prompt_never_asks_for_written_reasoning(agent_id):
    hits = sorted({m.group(0) for m in ASKS_FOR_WRITTEN_REASONING.finditer(render_static(agent_id))})
    assert hits == [], f"{agent_id} asks for written reasoning: {hits}"
