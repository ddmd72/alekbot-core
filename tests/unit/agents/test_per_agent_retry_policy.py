"""
Per-agent class-level RETRY_POLICY regression guards.

Pin which agents override the BaseAgent default. If a future edit
silently flips an ASYNC document agent or Router back to retry, this
test fails immediately — without these guards the regression would only
surface as duplicate user-facing artifacts in production.

Per:
  docs/04_solution_strategy/decisions/typed_retry_policy.md
"""

from __future__ import annotations

from src.agents.base_agent import BaseAgent
from src.agents.claude_deep_research_runner_agent import ClaudeDeepResearchRunnerAgent
from src.agents.core.router_agent import RouterAgent
from src.agents.doc_generator_agent import DocGeneratorAgent
from src.agents.doc_planner_agent import DocPlannerAgent
from src.agents.html_page_generator_agent import HtmlPageGeneratorAgent
from src.agents.pdf_generator_agent import PdfGeneratorAgent
from src.domain.retry_policy import (
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
)


class TestBaseAgentDefaultPolicy:
    def test_baseagent_uses_default_retry_policy(self):
        assert BaseAgent.RETRY_POLICY is DEFAULT_RETRY_POLICY


class TestRouterUsesNoRetry:
    """Router triage must stay fast — retry would push triage latency
    past the budget for which Router exists at all."""

    def test_router_class_attr_is_no_retry(self):
        assert RouterAgent.RETRY_POLICY is NO_RETRY_POLICY


class TestAsyncDocumentAgentsUseNoRetry:
    """ASYNC document generation runs in its own Cloud Task. A transient
    in-process retry would re-do the entire generation — paying for the
    full LLM token bill twice. Cloud Tasks queue retry covers transients
    at the right granularity."""

    def test_doc_planner_class_attr_is_no_retry(self):
        assert DocPlannerAgent.RETRY_POLICY is NO_RETRY_POLICY

    def test_doc_generator_class_attr_is_no_retry(self):
        assert DocGeneratorAgent.RETRY_POLICY is NO_RETRY_POLICY

    def test_pdf_generator_class_attr_is_no_retry(self):
        assert PdfGeneratorAgent.RETRY_POLICY is NO_RETRY_POLICY

    def test_html_page_generator_class_attr_is_no_retry(self):
        assert HtmlPageGeneratorAgent.RETRY_POLICY is NO_RETRY_POLICY


class TestClaudeDeepResearchRunnerUsesNoRetry:
    """Claude DR runner runs 10–25 minutes inside a Cloud Run Job —
    retrying doubles wall time and cost."""

    def test_class_attr_is_no_retry(self):
        assert ClaudeDeepResearchRunnerAgent.RETRY_POLICY is NO_RETRY_POLICY
