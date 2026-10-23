"""Every agent that takes an `execution_context` hands it to `BaseAgent._set_execution_context` (P9 D8).

`_call_llm` reads `self._agent_execution_context`. It defaults to `None`, and then there is no breaker accounting and no
cross-provider failover whatever `AgentProviderStrategy.STRATEGIES` declares for the agent: the first timeout or 429
raises `BothProvidersUnavailableError`. The context is opt-in per constructor, and an agent that keeps only
`self._llm = execution_context.provider` loses it without any error (the html_page incident, 2026-10-07; the five agents
fixed with this test). An agent that is built per call, by the indexer or by a job, is never seen by a graph walk, so
this check reads the classes themselves.

An agent that does not call it is on `ALLOWED_WITHOUT` with the reason. A stale entry fails.
"""
import ast
import importlib
import inspect
import pkgutil
import textwrap
from typing import Dict, List, Optional

import pytest

import src.agents
from src.agents.base_agent import BaseAgent

NO_REAL_FAILOVER = ("by-design no real failover: STRATEGIES fallback is {fallback}, so a context would add only breaker "
                    "accounting, a behaviour change that is an owner decision (decisions/html_page_fallback_context_wiring.md)")
OWNER_RULING = ("by-design no cross-provider fallback by owner ruling 2026-10-23 (P9 D8): memory quality over availability, so "
                "STRATEGIES[{strategy}] has fallback None and the agent writes {what} only with its own provider")

ALLOWED_WITHOUT: Dict[str, str] = {
    "ComputeAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "compute")'),
    "FactsMemoryAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "facts_memory")'),
    "ImageGenerationAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "image_generation")'),
    "MapsSearchAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "maps_search")'),
    "VideoGenerationAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "video_generation")'),
    "WebSearchLightAgent": NO_REAL_FAILOVER.format(fallback='None (agent_context_builder.py "web_search_light")'),
    "EmailSearchAgent": NO_REAL_FAILOVER.format(fallback='gemini, equal to its only allowed provider (agent_context_builder.py "email_search")'),
    "EmailClassificationAgent": NO_REAL_FAILOVER.format(fallback='gemini, equal to its only allowed provider (agent_context_builder.py "email_classifier")'),
    "ConsolidationAgent": OWNER_RULING.format(strategy='"consolidation"', what="facts"),
    "TutorExtractorAgent": OWNER_RULING.format(strategy='"tutor_extractor"', what="companion records"),
}


def hands_over_context(init_source: str) -> bool:
    """True when the constructor calls `_set_execution_context`, or passes `execution_context` on to `super().__init__`."""
    for node in ast.walk(ast.parse(textwrap.dedent(init_source))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "_set_execution_context":
            return True
        is_super_init = node.func.attr == "__init__" and isinstance(node.func.value, ast.Call) \
            and isinstance(node.func.value.func, ast.Name) and node.func.value.func.id == "super"
        passed = [a for a in node.args] + [k.value for k in node.keywords]
        if is_super_init and any(isinstance(a, ast.Name) and a.id == "execution_context" for a in passed):
            return True
    return False


def agents_taking_a_context() -> Dict[str, Optional[bool]]:
    """`{class name: hands the context over}` for every BaseAgent subclass with its own `__init__(…, execution_context, …)`."""
    found: Dict[str, Optional[bool]] = {}
    for module_info in pkgutil.walk_packages(src.agents.__path__, "src.agents."):
        module = importlib.import_module(module_info.name)
        for cls in vars(module).values():
            if inspect.isclass(cls) and issubclass(cls, BaseAgent) and cls.__module__ == module.__name__ \
                    and "__init__" in cls.__dict__ and "execution_context" in inspect.signature(cls.__init__).parameters:
                found[cls.__name__] = hands_over_context(inspect.getsource(cls.__init__))
    return found


def problems(found: Dict[str, Optional[bool]], allowed: Dict[str, str]) -> List[str]:
    result = [f"{name} takes an execution_context and never calls self._set_execution_context(execution_context): its declared "
              f"provider fallback cannot run (add the call after super().__init__, or list it in ALLOWED_WITHOUT with a reason)"
              for name, ok in sorted(found.items()) if not ok and name not in allowed]
    result += [f"ALLOWED_WITHOUT lists {name}, which {'now calls it' if name in found else 'no longer takes an execution_context'}: remove the entry"
               for name in sorted(allowed) if found.get(name) is not False]
    result += [f"ALLOWED_WITHOUT entry {name} has no reason" for name, reason in sorted(allowed.items()) if len(reason.split()) < 6]
    return result


def test_every_agent_with_an_execution_context_hands_it_to_the_base_class():
    found = agents_taking_a_context()

    assert len(found) >= 20, f"the scan found only {sorted(found)}: the discovery is broken"
    assert not problems(found, ALLOWED_WITHOUT), "\n".join(problems(found, ALLOWED_WITHOUT))


@pytest.mark.parametrize("name", sorted(ALLOWED_WITHOUT))
def test_every_allowed_reason_uses_the_vocabulary(name):
    assert ALLOWED_WITHOUT[name].startswith("by-design")


@pytest.mark.parametrize("fixed", ["WebSearchAgent", "TasksAgent", "DomainResearcherAgent", "TutorAgent", "LelikSummarizerAgent"])
def test_the_five_fixed_agents_hand_the_context_over(fixed):
    assert agents_taking_a_context()[fixed] is True


# ----------------------------------------------------------------------------- the check, on inline sources

WITH_CALL = """
def __init__(self, config, execution_context):
    super().__init__(config)
    self._set_execution_context(execution_context)
    self._llm = execution_context.provider
"""
WITHOUT = """
def __init__(self, config, execution_context):
    super().__init__(config)
    self._llm = execution_context.provider
"""
DELEGATES = """
def __init__(self, config, execution_context):
    super().__init__(config, execution_context)
"""
SUPER_WITHOUT_CONTEXT = """
def __init__(self, config, execution_context):
    super().__init__(config)
    helper(execution_context)
"""


def test_the_constructor_check_sees_the_call_and_a_delegation_to_the_parent():
    assert hands_over_context(WITH_CALL) is True
    assert hands_over_context(DELEGATES) is True


def test_the_constructor_check_refuses_a_constructor_that_only_keeps_the_provider():
    assert hands_over_context(WITHOUT) is False
    assert hands_over_context(SUPER_WITHOUT_CONTEXT) is False


def test_an_agent_that_does_not_hand_the_context_over_is_reported_unless_allowed():
    found = {"Good": True, "Bad": False}

    assert "Bad takes an execution_context and never calls" in problems(found, {})[0]
    assert problems(found, {"Bad": "by-design no real failover, fallback is None here"}) == []


def test_a_stale_entry_and_an_entry_without_a_reason_are_reported():
    found = {"Good": True}

    result = problems(found, {"Good": "by-design no real failover, fallback is None", "Gone": "by-design a reason of enough words here"})

    assert any("Good, which now calls it" in p for p in result)
    assert any("Gone, which no longer takes an execution_context" in p for p in result)
    assert problems({"Bad": False}, {"Bad": "by-design"}) == ["ALLOWED_WITHOUT entry Bad has no reason"]
