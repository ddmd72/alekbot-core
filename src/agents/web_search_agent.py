"""
Web Search Agent
================

Specialized agent for web search using Gemini Grounding.
Uses dedicated Gemini instance with Google Search grounding tool.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..ports.llm_service import AgentExecutionContext
from ..services.prompt_builder import PromptBuilder
from ..ports.llm_service import Message, MessagePart, LLMRequest
from ..utils.logger import logger


class WebSearchAgent(BaseAgent):
    """
    Agent responsible for web search queries.
    
    Capabilities:
    - Web search using Gemini Grounding
    - Current events and news
    - External facts retrieval
    
    Uses dedicated LLM instance with Google Search tool.
    """
    
    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        grounding_tool: object,
        prompt_builder: Optional[PromptBuilder] = None,
        user_id: Optional[str] = None
    ):
        """
        Initialize Web Search Agent.
        
        Args:
            config: Agent configuration (includes model name)
            execution_context: Execution context with provider + model
            grounding_tool: Google Search grounding tool
        """
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._grounding_tool = grounding_tool
        self.prompt_builder = prompt_builder
        self.user_id = user_id
        
        logger.info(
            f"🌐 WebSearchAgent initialized "
            f"(model={self.model_name})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        Determine if this agent can handle the message.
        
        WebSearchAgent is an executor, not a decision maker.
        SmartResponseAgent's LLM already decided to delegate here.
        
        We only validate:
        - Intent must be QUERY
        - Payload must have 'query'
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if agent can process this message
        """
        logger.debug(
            f"🌐 [WebSearchAgent] can_handle check: "
            f"intent={message.intent}, payload={message.payload}"
        )
        
        # Check intent
        if message.intent != AgentIntent.QUERY:
            logger.debug("🌐 [WebSearchAgent] can_handle=False (wrong intent)")
            return False
        
        # Check query exists
        query = message.payload.get("query", "")
        if not query:
            logger.debug("🌐 [WebSearchAgent] can_handle=False (no query in payload)")
            return False
        
        logger.debug("🌐 [WebSearchAgent] can_handle=True (valid query)")
        return True
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute web search using Gemini Grounding.
        
        Args:
            message: Agent message containing search query
            
        Returns:
            Agent response with search results
        """
        query = message.payload.get("query", "")
        
        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload"
            )
        
        logger.info(f"🌐 [WebSearchAgent] Starting search: '{query[:50]}...'")
        start_time = time.time()
        
        try:
            # Inject current date/time context
            current_time_str = datetime.now(timezone.utc).strftime(
                '%A, %d %B %Y, %H:%M %Z'
            )
            
            # Build search prompt (v3 token-based if available)
            if self.prompt_builder:
                # SESSION_27: account_id should be resolved from RequestContext
                # self.user_id may not have "account-" prefix
                prompt = await self.prompt_builder.build_for_agent(
                    agent_type="websearch",
                    user_id=self.user_id,
                    account_id=None,  # Let PromptBuilder resolve from RequestContext
                    routing_metadata=None,
                )
                augmented_query = (
                    f"// Context Injection\n"
                    f"current_date = '{current_time_str}'\n"
                    f"user_query = '{query}'\n\n"
                    f"{prompt}\n\n"
                    "// Execute\n"
                    "SearchAgent.run(user_query)"
                )
            else:
                augmented_query = (
                    f"// Context Injection\n"
                    f"current_date = '{current_time_str}'\n"
                    f"user_query = '{query}'\n\n"
                    "class SearchAgent extends GoogleSearchAgent {\n"
                    "  archetype: 'Meticulous Researcher. Loves exhaustive lists. Hates ambiguity.'\n\n"
                    "  cognitive_process {\n"
                    "    steps: [\n"
                    "      '1. ANALYZE: Extract Object and Criteria from user_query.',\n"
                    "      '2. EXECUTE: Perform grounding search using Google Search.',\n"
                    "      '3. VERIFY: Check results against Criteria.',\n"
                    "      '4. REFINE: If insufficient, refine search and retry.',\n"
                    "      '5. COMPILE: Aggregate ALL non-contradictory results.',\n"
                    "      '6. DELIVER: Present final list with summary.'\n"
                    "    ]\n"
                    "  }\n\n"
                    "  output_format {\n"
                    "    style: 'Slack mrkdwn (no headers, use *bold*)'\n"
                    "    structure: 'List of Options -> Summary'\n"
                    "  }\n"
                    "}\n\n"
                    "// Execute\n"
                    "SearchAgent.run(user_query)"
                )
            
            logger.debug("   → Calling Gemini with grounding...")
            llm_start = time.time()

            request = LLMRequest(
                model_name=self.model_name,
                system_instruction="",
                messages=[Message(role="user", parts=[MessagePart(text=augmented_query)])],
                tools=[self._grounding_tool],
                temperature=0.7
            )
            response = await self._llm.generate_content(request=request)
            
            llm_duration = time.time() - llm_start
            logger.debug(f"   ✓ Gemini responded in {llm_duration:.2f}s")
            
            result_text = response.text
            
            if not result_text or result_text == "No relevant information found on the web.":
                logger.warning(f"⚠️ [WebSearchAgent] No results for: '{query[:50]}...'")
                return AgentResponse(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    status="partial",
                    result=result_text or "No relevant information found on the web.",
                    confidence=0.0,
                    metadata={
                        "total_duration_ms": int((time.time() - start_time) * 1000),
                        "llm_duration_ms": int(llm_duration * 1000)
                    }
                )
            
            total_duration = time.time() - start_time
            
            logger.info(
                f"✅ [WebSearchAgent] Completed search in {total_duration:.2f}s "
                f"(result length: {len(result_text)} chars)"
            )
            
            # Estimate confidence based on result length and quality
            confidence = min(1.0, len(result_text) / 500) if result_text else 0.0
            
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
                confidence=confidence,
                metadata={
                    "total_duration_ms": int(total_duration * 1000),
                    "llm_duration_ms": int(llm_duration * 1000),
                    "result_length": len(result_text),
                    "model": self.model_name
                }
            )
            
        except Exception as e:
            logger.error(f"❌ [WebSearchAgent] Error: {e}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Web search failed: {str(e)}"
            )
    
    def _get_alternative_agents(self) -> list[str]:
        """Suggest alternative agents if this one cannot handle the request."""
        return ["memory_search_agent", "reasoning_agent"]
