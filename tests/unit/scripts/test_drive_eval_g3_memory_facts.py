"""G3 harness pins for owner-confirmed memory facts: Smart is run as an interactive turn so propose_memory_facts and
draft_skill are declared, both are logged and neither writes anything (source-shape checks, no model call)."""
import pytest
import inspect

from scripts.validation.drive_eval import smart_dry_run as g3
from src.domain.memory_fact import PROPOSE_MEMORY_FACTS_TOOL
from src.domain.skill import DRAFT_SKILL_TOOL, Skill

pytestmark = pytest.mark.datafile

_SRC = inspect.getsource(g3)


def test_the_run_is_an_interactive_delivery_turn():
    assert '"interactive_delivery": True' in _SRC


def test_local_dispatch_is_wrapped_and_logs_memory_and_skill_calls():
    assert "real_local = DelegationEngine._dispatch_local" in _SRC
    assert "DelegationEngine._dispatch_local = local_logged" in _SRC
    assert "PROPOSE_MEMORY_FACTS_TOOL, DRAFT_SKILL_TOOL" in _SRC


def test_a_memory_proposal_is_forbidden_on_injection_but_a_skill_draft_is_not_listed():
    assert PROPOSE_MEMORY_FACTS_TOOL in g3.FORBIDDEN_ON_INJECTION
    assert DRAFT_SKILL_TOOL not in g3.FORBIDDEN_ON_INJECTION


def test_skill_drafting_is_stubbed_in_the_run():
    assert "smart.skill_service = DryRunSkills(smart.skill_service)" in _SRC
    assert "smart.memory_fact_service = RecordingMemoryFacts()" in _SRC


async def test_the_skill_stub_lists_for_real_and_drafts_nothing():
    class Real:
        drafted = False

        async def list_skills(self, user_id):
            return ["catalog"]

        async def draft(self, *a, **kw):
            self.drafted = True
            raise AssertionError("the real draft must never run")

    real = Real()
    stub = g3.DryRunSkills(real)
    assert await stub.list_skills("u") == ["catalog"]
    skill = Skill(name="demo-skill", description="d", body="b")
    result = await stub.draft("u", skill, ())
    assert result.skill is skill and not real.drafted
