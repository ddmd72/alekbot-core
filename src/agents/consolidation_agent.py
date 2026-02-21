"""
Consolidation Agent v3 (Deliberate Fact Management)
====================================================

Specialized agent for deliberate fact curation using 4D taxonomy.
The "Life Chronicler" - transforms conversations into high-quality biographical memory.

Session 2026-02-16: Phase 3 - Multi-turn tool use integration
- Added FactManagementPort for deliberate operations (search, create, update, merge, discard)
- Multi-turn loop for agent-driven fact management decisions
- 8-step deliberate cognitive process (EXTRACT → CLASSIFY → SEARCH → ANALYZE → DECIDE → EXECUTE → VERIFY → REPORT)
"""

import re
import json
import uuid
import time
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from dataclasses import dataclass

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..domain.entities import FactEntity, FactType
from ..domain.request_context import get_effective_account_id, get_current_user_id
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_management_port import FactManagementPort
from ..ports.llm_service import LLMService, LLMResponse, ToolCall, Message, MessagePart, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..utils.debug_logger import get_debug_logger
from ..ports.llm_service import AgentExecutionContext
from ..ports.fact_write_port import FactWritePort


@dataclass
class ToolResponse:
    """Internal container for fact management tool results."""
    name: str
    result_str: str


class ConsolidationAgent(BaseAgent):
    """
    Agent responsible for deliberate fact curation with 4D taxonomy.
    
    Capabilities:
    - 8-step deliberate cognitive process
    - Awareness-first strategy (search before create)
    - Multi-turn tool use (search → analyze → decide → execute)
    - 4D fact classification (Domain × Temporal × State × Priority)
    - Lifecycle-managed facts with TTL
    
    Uses powerful LLM for synthesis and deliberate decision-making.
    """
    
    MAX_CONSOLIDATION_TURNS = 10  # Max iterations for deliberate process
    
    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        fact_write_service: FactWritePort,
        fact_management_port: Optional[FactManagementPort] = None,  # NEW: Phase 3
        prompt_version: str = "v3",  # Changed default to v3
        prompt_builder: Optional[PromptBuilderPort] = None,
        facts_limit: int = 50,
        principles_limit: int = 15
    ):
        """
        Initialize Consolidation Agent v3.
        
        Session 2026-02-16: Phase 3 - Deliberate Fact Management
        Added fact_management_port for multi-turn tool use (search, create, update, merge, discard).
        
        Args:
            config: Agent configuration (includes LLM model)
            execution_context: Execution context with provider + model
            repository: Repository for reading/writing (legacy path)
            embedding_service: Service for generating embeddings (legacy)
            fact_write_service: Service for writing facts (legacy path v2)
            fact_management_port: Port for deliberate fact operations (NEW v3)
            prompt_version: Version of the prompt template ("v2" or "v3")
            prompt_builder: Prompt builder for cache invalidation
            facts_limit: Biographical cache limit
            principles_limit: Principles cache limit
        """
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._repo = repository
        self._embedding = embedding_service
        self._fact_write_service = fact_write_service
        self._fact_management = fact_management_port  # NEW
        self.prompt_version = prompt_version  # NEW
        self.prompt_builder = prompt_builder
        self.user_id = None  # Consolidation is system-level, no user context
        self.facts_limit = facts_limit
        self.principles_limit = principles_limit

        logger.info(
            f"👨‍🏫 ConsolidationAgent initialized "
            f"(version={prompt_version}, model={self.model_name}, "
            f"facts_limit={facts_limit}, principles_limit={principles_limit})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        Determine if this agent can handle the message.
        
        Handles messages with:
        - Intent: DELEGATE
        - Task: "consolidate" or "synthesize"
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if agent can process this message
        """
        if message.intent != AgentIntent.DELEGATE:
            return False
        
        payload = message.payload
        task = payload.get("task", "").lower()
        
        return task in ["consolidate", "synthesize", "librarian"]
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute consolidation process.
        
        Supports:
        - v3: Multi-turn tool use with FactManagementPort (deliberate curation)
        - v2: Legacy single-shot LLM call (backward compatibility)
        
        Args:
            message: Agent message with consolidation request
            
        Returns:
            Agent response with consolidation results
        """
        user_id = message.context.get("user_id")
        
        if not user_id:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Missing user_id in context"
            )
        
        logger.info(f"👨‍🏫 [ConsolidationAgent] Starting consolidation for user {user_id[:8]}...")
        start_time = time.time()
        
        try:
            payload = message.payload
            messages = payload.get("messages", [])
            observations = payload.get("observations", [])

            # Resolve account_id from RequestContext
            account_id = get_effective_account_id()
            if not account_id:
                raise ValueError(
                    "Missing account_id in RequestContext. "
                    "ConversationHandler must set RequestContext before calling consolidation."
                )
            
            # Load biographical context
            bio_context_raw = payload.get("biographical_context")
            if not bio_context_raw:
                try:
                    logger.info("📚 Context missing in payload, loading from cache...")
                    bio_context_raw = await self._repo.get_biographical_context_cached(limit=100)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to load biographical context from cache: {e}")
                    bio_context_raw = []
            
            # Route to v3 or v2 based on configuration
            if self.prompt_version == "v3" and self._fact_management:
                logger.info("🧠 [ConsolidationAgent] Using v3 deliberate process")
                results = await self._execute_deliberate_process_v3(
                    messages=messages,
                    user_id=user_id,
                    account_id=account_id,
                    bio_context_raw=bio_context_raw
                )
            else:
                logger.info("🧠 [ConsolidationAgent] Using v2 legacy process")
                if messages:
                    results = await self._synthesize_session_facts_v2(
                        messages, user_id, account_id, bio_context_raw
                    )
                elif observations:
                    logger.warning(
                        "⚠️ [ConsolidationAgent] Observation-based flow deprecated; skipping."
                    )
                    results = {"new_facts": [], "new_anchors": []}
                else:
                    logger.info("ℹ️  [ConsolidationAgent] No data to process")
                    return AgentResponse.success(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        result={"new_facts": 0, "new_anchors": 0, "message": "No data"},
                        confidence=1.0,
                        metadata={"processed": 0}
                    )
            
            if not results:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="Failed to parse consolidation results"
                )
            
            # Handle v3 results (operations list)
            if "operations" in results:
                operations = results["operations"]
                new_facts_count = sum(1 for op in operations if op["action"] in ["CREATE", "UPDATE", "MERGE"])
                
                logger.info(
                    f"✅ [ConsolidationAgent] v3 Completed: {len(operations)} operations "
                    f"({new_facts_count} facts affected)"
                )
                
                # Trigger cache refresh
                logger.info(f"🔄 Triggering biographical cache refresh for account {account_id[:8]}...")
                await self._repo.refresh_biographical_context_cache(
                    owner_id=account_id,
                    facts_limit=self.facts_limit,
                    principles_limit=self.principles_limit
                )
                
                # Invalidate PromptBuilder cache
                if self.prompt_builder:
                    self.prompt_builder.invalidate_biographical_cache(account_id)
                    logger.info(f"♻️  Invalidated PromptBuilder cache for account {account_id[:8]}")
                
                total_duration = time.time() - start_time
                return AgentResponse.success(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    result={
                        "operations": len(operations),
                        "facts_affected": new_facts_count,
                        "message": f"Processed {len(operations)} operations"
                    },
                    confidence=1.0,
                    metadata={
                        "total_duration_ms": int(total_duration * 1000),
                        "operations": operations,
                        "version": "v3"
                    }
                )
            
            # Handle v2 results (legacy)
            llm_facts = self._sanitize_ids(results.get("new_facts", []))
            llm_anchors = self._sanitize_ids(results.get("new_anchors", []))
            
            if len(llm_facts) == 0 and len(llm_anchors) == 0:
                if messages:
                    logger.info("ℹ️  [ConsolidationAgent] Session consolidation yielded 0 new facts (valid).")
                elif observations:
                    logger.error(
                        f"❌ [ConsolidationAgent] LLM returned empty result for observations. "
                        f"Observations preserved for retry."
                    )
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        error="LLM returned invalid format (empty). Observations preserved for retry."
                    )
            
            # Save facts via FactWriteService (v2 path)
            all_facts_data = llm_facts + llm_anchors
            saved_count, skipped_count = await self._fact_write_service.add_facts_batch(
                account_id=account_id,
                user_id=user_id,
                facts_data=all_facts_data
            )
            
            # Archive observations
            if observations:
                logger.debug(f"   → Archiving {len(observations)} observations...")
                await self._repo.archive_observations(
                    [obs['id'] for obs in observations],
                    owner_id=user_id
                )
            
            total_duration = time.time() - start_time
            logger.info(
                f"✅ [ConsolidationAgent] v2 Completed in {total_duration:.2f}s "
                f"(facts={len(llm_facts)}, anchors={len(llm_anchors)})"
            )

            # Trigger cache refresh
            await self._repo.refresh_biographical_context_cache(
                owner_id=account_id,
                facts_limit=self.facts_limit,
                principles_limit=self.principles_limit
            )
            
            if self.prompt_builder:
                self.prompt_builder.invalidate_biographical_cache(account_id)
            
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={
                    "new_facts": len(llm_facts),
                    "new_anchors": len(llm_anchors),
                    "observations_processed": len(observations),
                    "message": f"Added {len(llm_facts)} facts and {len(llm_anchors)} anchors"
                },
                confidence=1.0,
                metadata={
                    "total_duration_ms": int(total_duration * 1000),
                    "llm_payload": {"new_facts": llm_facts, "new_anchors": llm_anchors},
                    "version": "v2"
                }
            )
            
        except Exception as e:
            logger.error(f"❌ [ConsolidationAgent] Error: {e}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Consolidation failed: {str(e)}"
            )
    
    async def _execute_deliberate_process_v3(
        self,
        messages: List[Dict],
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict]
    ) -> Dict[str, Any]:
        """
        Execute deliberate fact management process (v3) with multi-turn tool use.
        
        8-Step Process:
        1. EXTRACT: Parse conversation → candidate facts (done by LLM)
        2. CLASSIFY: Assign 4D taxonomy (done by LLM)
        3. SEARCH: Query existing facts via search_existing_facts tool
        4. ANALYZE: Compare candidate vs existing (done by LLM)
        5. DECIDE: UPDATE/CREATE/MERGE/DISCARD (done by LLM)
        6. EXECUTE: Call fact management tools
        7. VERIFY: Check tool results
        8. REPORT: Summarize operations
        
        Returns:
            Dict with "operations" list
        """
        # Prepare conversation history
        structured_conversation = self._prepare_structured_conversation(messages)
        conversation_history = [
            {
                "role": item.get("role", "user"),
                "content": item.get("content", ""),
                "timestamp": item.get("timestamp", "")
            }
            for item in structured_conversation
            if item.get("content")
        ]
        
        # Prepare existing anchors
        existing_anchors_list = await self._get_existing_anchors_list(account_id)
        biographical_facts = bio_context_raw or []
        
        # Build system prompt via PromptBuilder (loads CONSOLIDATION_V3_PROMPT.txt)
        system_prompt = await self.prompt_builder.build_for_agent(
            agent_type="consolidation",
            user_id=user_id,
            account_id=account_id,
            routing_metadata=None,
            biographical_facts=biographical_facts,
            conversation_history=conversation_history
        )
        
        # Debug logging
        debug_logger = get_debug_logger()
        debug_logger.log_prompt(
            agent_name="consolidation_v3",
            prompt=system_prompt,
            system_instruction="Multi-turn deliberate consolidation with tool use",
            metadata={"user_id": user_id[:8], "account_id": account_id[:8]}
        )
        
        # Multi-turn tool use loop
        # Initialize history with user message to maintain [user, model, user, ...] pattern
        history: List[Message] = [
            Message(role="user", parts=[MessagePart(text="Begin deliberate consolidation.")])
        ]
        total_tokens = 0
        
        for turn in range(self.MAX_CONSOLIDATION_TURNS):
            logger.info(
                f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1}/{self.MAX_CONSOLIDATION_TURNS}"
            )
            
            # Build LLM request
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=history,
                tools=self._get_tool_declarations(),
                temperature=0.7
            )
            
            # Call LLM
            response: LLMResponse = await self._llm.generate_content(request=request)
            
            if response.usage_metadata:
                total_tokens += response.usage_metadata.total_tokens
            
            # No tool calls → final report (Step 8)
            if not response.tool_calls:
                logger.info(f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1} - Final report received")
                
                # Parse final JSON report
                operations = self._parse_operations_report(response.text or "")
                
                debug_logger.log_response(
                    agent_name="consolidation_v3",
                    response=response.text or "",
                    metadata={
                        "user_id": user_id[:8],
                        "tokens": total_tokens,
                        "operations": len(operations)
                    }
                )
                
                return {"operations": operations}
            
            # Tool calls → execute (Step 6)
            logger.info(
                f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1} - Executing {len(response.tool_calls)} tools"
            )
            
            # Add model's tool calls to history
            if response.raw_content:
                history.append(Message(role="model", parts=[], raw_content=response.raw_content))
            else:
                history.append(Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls]
                ))
            
            # Execute tools
            tool_responses = await self._execute_fact_management_tools(
                tool_calls=response.tool_calls,
                user_id=user_id,
                account_id=account_id
            )
            
            # Add tool responses to history
            history.append(Message(
                role="user",
                parts=[
                    MessagePart(tool_response={
                        "name": tr.name,
                        "response": {"result": tr.result_str}
                    })
                    for tr in tool_responses
                ]
            ))
        
        # Max turns reached without final report
        logger.warning(f"⚠️ [ConsolidationAgent] Max turns reached without final report")
        return {"operations": []}
    
    async def _execute_fact_management_tools(
        self,
        tool_calls: List[ToolCall],
        user_id: str,
        account_id: str
    ) -> List[ToolResponse]:
        """Execute fact management tools via FactManagementPort."""
        results: List[ToolResponse] = []
        
        for tool_call in tool_calls:
            logger.info(f"🔧 [ConsolidationAgent] Executing tool: {tool_call.name}")
            
            try:
                if tool_call.name == "search_existing_facts":
                    # Extract new 3-key format parameters
                    keywords = tool_call.args.get("keywords", [])
                    primary_query = tool_call.args.get("primary_query", "")
                    alternative_query = tool_call.args.get("alternative_query", "")
                    limit = tool_call.args.get("limit", 20)
                    
                    logger.info(
                        f"   🔍 search_existing_facts("
                        f"keywords={keywords[:3]}{'...' if len(keywords) > 3 else ''}, "
                        f"primary='{primary_query[:40]}...', "
                        f"alternative='{alternative_query[:40] if alternative_query else 'N/A'}...', "
                        f"limit={limit})"
                    )
                    
                    result = await self._fact_management.search_existing_facts(
                        keywords=keywords,
                        primary_query=primary_query,
                        alternative_query=alternative_query,
                        limit=limit
                    )
                    results.append(ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    ))
                
                elif tool_call.name == "create_fact":
                    # Extract fact_attributes and inject account_id/user_id
                    fact_attributes = tool_call.args.get("fact_attributes", {})
                    fact_attributes["account_id"] = account_id
                    fact_attributes["user_id"] = user_id
                    
                    result = await self._fact_management.create_fact(
                        content=tool_call.args.get("content", ""),
                        metadata=fact_attributes
                    )
                    results.append(ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    ))
                
                elif tool_call.name == "update_fact":
                    result = await self._fact_management.update_fact(
                        fact_id=tool_call.args.get("fact_id", ""),
                        updates=tool_call.args.get("updates", {})
                    )
                    results.append(ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    ))
                
                elif tool_call.name == "merge_facts":
                    # Extract fact_attributes and inject account_id/user_id
                    fact_attributes = tool_call.args.get("fact_attributes", {})
                    fact_attributes["account_id"] = account_id
                    fact_attributes["user_id"] = user_id
                    
                    result = await self._fact_management.merge_facts(
                        fact_ids=tool_call.args.get("fact_ids", []),
                        merged_content=tool_call.args.get("merged_content", ""),
                        metadata=fact_attributes
                    )
                    results.append(ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    ))
                
                else:
                    logger.warning(f"⚠️ Unknown tool: {tool_call.name}")
                    results.append(ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps({"error": f"Unknown tool: {tool_call.name}"})
                    ))
            
            except Exception as e:
                logger.error(f"❌ Tool {tool_call.name} failed: {e}", exc_info=True)
                results.append(ToolResponse(
                    name=tool_call.name,
                    result_str=json.dumps({"error": str(e)}, ensure_ascii=False)
                ))
        
        return results
    
    def _get_tool_declarations(self) -> List[Dict[str, Any]]:
        """
        Build tool declarations for ConsolidationAgent v3.
        
        4 tools from CONSOLIDATION_V3_PROMPT.txt:
        - search_existing_facts
        - create_fact
        - update_fact
        - merge_facts
        
        Note: discard_candidate removed - DISCARD is an LLM decision (not a tool call).
        LLM simply doesn't call create/update/merge for discarded candidates.
        """
        return [
            {
                "name": "search_existing_facts",
                "description": "Search existing facts using multi-vector RRF strategy (no semantic dedup - exact duplicates only). NO domain filter - returns all relevant facts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Domain keywords (nouns, names, objects, places, domain terms). Extract key entities from candidate fact."
                        },
                        "primary_query": {
                            "type": "string",
                            "description": "Primary semantic search phrase (natural language, full context of candidate fact)"
                        },
                        "alternative_query": {
                            "type": "string",
                            "description": "Alternative phrasing for diversity (optional, different angle or synonyms)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return (default: 20)"
                        }
                    },
                    "required": ["keywords", "primary_query"]
                }
            },
            {
                "name": "create_fact",
                "description": "Create NEW fact when candidate is orthogonal or new entity",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Fact text (self-contained sentence)"
                        },
                        "fact_attributes": {
                            "type": "object",
                            "description": "Fact attributes (MUST include: domain, temporal_class, state, context_priority, tags). Separate: tags (general keywords) vs metadata (structured data)",
                            "properties": {
                                "domain": {"type": "string", "description": "FactDomain (BIOGRAPHICAL, HEALTH, SKILL, etc.)"},
                                "temporal_class": {"type": "string", "description": "PERMANENT/STABLE/DYNAMIC/EPHEMERAL"},
                                "state": {"type": "string", "description": "CURRENT/ARCHIVED/SUPERSEDED/INVALIDATED"},
                                "context_priority": {"type": "string", "description": "CRITICAL/HIGH/MEDIUM/LOW/ARCHIVAL"},
                                "ttl_days": {"type": "integer", "description": "Lifecycle TTL (auto-calculated from temporal_class)"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "General classifications (domain keywords, dates)"},
                                "metadata": {"type": "object", "description": "Structured data (numeric values, specific dates, details)"},
                                "context": {"type": "string", "description": "Temporal context (e.g., 'Q1 2026 project')"},
                                "reported_date": {"type": "string", "description": "ISO timestamp when recorded"}
                            },
                            "required": ["domain", "temporal_class", "context_priority", "tags"]
                        }
                    },
                    "required": ["content", "fact_attributes"]
                }
            },
            {
                "name": "update_fact",
                "description": "Update EXISTING fact (enrichment or new data point)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact_id": {
                            "type": "string",
                            "description": "UUID of fact to update"
                        },
                        "updates": {
                            "type": "object",
                            "description": "Fields to update (content, tags, state, reported_date)",
                            "properties": {
                                "content": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "temporal_class": {"type": "string"},
                                "state": {"type": "string"},
                                "reported_date": {"type": "string"}
                            }
                        }
                    },
                    "required": ["fact_id", "updates"]
                }
            },
            {
                "name": "merge_facts",
                "description": "Consolidate multiple facts into one enriched fact",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of UUIDs to merge"
                        },
                        "merged_content": {
                            "type": "string",
                            "description": "New combined text"
                        },
                        "fact_attributes": {
                            "type": "object",
                            "description": "Attributes for new merged fact (MUST include: domain, temporal_class, state, context_priority, tags)"
                        }
                    },
                    "required": ["fact_ids", "merged_content", "fact_attributes"]
                }
            }
        ]
    
    def _parse_operations_report(self, response_text: str) -> List[Dict[str, Any]]:
        """
        Parse final operations report from LLM (Step 8).
        
        Expected JSON format:
        {
            "operations": [
                {"action": "UPDATE", "fact_id": "...", "reason": "..."},
                {"action": "CREATE", "fact_id": "...", "reason": "..."},
                {"action": "DISCARD", "reason": "..."}
            ]
        }
        """
        try:
            # Try to extract JSON from markdown code block
            json_match = re.search(
                r"```json\s*(\{.*?\})\s*```",
                response_text,
                re.DOTALL
            )
            if json_match:
                data = json.loads(json_match.group(1))
                return data.get("operations", [])

            # Try direct JSON parsing
            try:
                data = json.loads(response_text)
                return data.get("operations", [])
            except json.JSONDecodeError:
                pass

            # LLM returned plain text (e.g. narrative explanation) — extract
            # any embedded JSON object as fallback
            embedded = re.search(r"\{.*\"operations\".*\}", response_text, re.DOTALL)
            if embedded:
                data = json.loads(embedded.group(0))
                return data.get("operations", [])

            # Plain text response — tools already executed, report is cosmetic
            logger.warning(
                "⚠️ [ConsolidationAgent] Final report was plain text (not JSON). "
                "DB operations completed normally via tool calls."
            )
            return []

        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ [ConsolidationAgent] Could not parse operations report: {e}")
            return []
    
    # ========================================================================
    # Legacy v2 methods (backward compatibility)
    # ========================================================================
    
    def _build_conversation_text(self, messages: List[Dict]) -> str:
        """Build conversation text from messages (LEGACY for plain text format)."""
        conv_text = ""
        for msg in messages:
            role = msg.get("role") or msg.get("user_id") or "unknown"

            # Normalized role mapping
            if role == "user":
                normalized_role = "USER"
            else:
                normalized_role = "ASSISTANT"

            text = msg.get("text") or ""
            if not text and "parts" in msg:
                text = " ".join([p.get("text", "") for p in msg["parts"]])

            # Timestamp handling
            timestamp = msg.get('timestamp') or msg.get('created_at')
            if timestamp:
                try:
                    dt = datetime.fromtimestamp(timestamp) if isinstance(timestamp, (int, float)) else datetime.fromisoformat(str(timestamp))
                    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    conv_text += f"{normalized_role} ({time_str}): {text}\n"
                except Exception:
                     conv_text += f"{normalized_role}: {text}\n"
            else:
                conv_text += f"{normalized_role}: {text}\n"
        return conv_text

    def _prepare_structured_conversation(self, messages: List[Dict]) -> List[Dict]:
        """
        Prepare structured conversation for VariableFormatter.

        Returns list of dicts with normalized structure:
        [
            {"role": "user", "content": "text", "timestamp": "2026-01-31 11:44:16"},
            {"role": "assistant", "content": "text", "timestamp": "2026-01-31 11:44:16"}
        ]
        """
        structured = []
        for msg in messages:
            role = msg.get("role") or msg.get("user_id") or "unknown"

            # Normalized role mapping
            if role == "user":
                normalized_role = "user"
            else:
                normalized_role = "assistant"

            # Extract text
            text = msg.get("text") or ""
            if not text and "parts" in msg:
                text = " ".join([p.get("text", "") for p in msg["parts"]])

            # Timestamp handling
            timestamp = msg.get('timestamp') or msg.get('created_at')
            time_str = ""
            if timestamp:
                try:
                    if isinstance(timestamp, (int, float)):
                        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    else:
                        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                        dt = dt.astimezone(timezone.utc)
                    time_str = dt.strftime("%b %d, %H:%M UTC")
                except Exception:
                    time_str = ""

            structured.append({
                "role": normalized_role,
                "content": text,
                "timestamp": time_str
            })

        return structured

    async def _format_existing_anchors(self, user_id: str) -> str:
        """Format existing anchors for prompt injection (LEGACY for Groovy format)."""
        existing_anchors_list = await self._repo.get_active_facts(user_id, tags=["anchor"])
        # Format anchors for prompt (joining with commas for groovy array)
        return ",\n".join([f'            "{a.text}"' for a in existing_anchors_list])

    async def _get_existing_anchors_list(self, account_id: str) -> List[str]:
        """
        Get existing anchors as list (for VariableFormatter).
        Optimized: uses cached biographical facts instead of Firestore query.
        """
        # Get ALL facts from cache (faster than Firestore query)
        all_facts = await self._repo.get_biographical_context_cached(owner_id=account_id, limit=100)
        # Filter anchors locally
        return [f.get("text") for f in all_facts if "anchor" in f.get("tags", [])]

    async def _synthesize_session_facts_v2(
        self,
        messages: List[Dict],
        user_id: str,
        account_id: str,
        biographical_context: List[Dict] = None
    ) -> Dict[str, Any]:
        """Synthesize facts from raw messages using v2 prompt (legacy)."""
        # 1. Prepare conversation history
        structured_conversation = self._prepare_structured_conversation(messages)
        conversation_history = [
            {
                "role": item.get("role", "user"),
                "content": item.get("content", ""),
                "timestamp": item.get("timestamp", "")
            }
            for item in structured_conversation
            if item.get("content")
        ]

        # 2. Prepare existing anchors
        existing_anchors_list = await self._get_existing_anchors_list(account_id)
        biographical_facts = biographical_context or []
        if existing_anchors_list:
            biographical_facts = list(biographical_facts)
            biographical_facts.append({
                "text": "Existing anchors: " + ", ".join(existing_anchors_list),
                "type": "PRINCIPLE",
                "source": "consolidation"
            })

        # 3. Assemble prompt via PromptBuilder (loads v2 prompt)
        prompt = await self.prompt_builder.build_for_agent(
            agent_type="consolidation",
            user_id=user_id,
            account_id=account_id,
            routing_metadata=None,
            biographical_facts=biographical_facts,
            conversation_history=conversation_history
        )

        # DEBUG: Log prompt and response
        debug_logger = get_debug_logger()
        debug_logger.log_prompt(
            agent_name="consolidation_v2",
            prompt=prompt,
            system_instruction="You are Life Chronicler. Return only valid JSON.",
            metadata={"user_id": user_id[:8] if user_id else "unknown", "account_id": account_id[:8] if account_id else "unknown"}
        )

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction="You are Life Chronicler. Return only valid JSON.",
            messages=[Message(role="user", parts=[MessagePart(text=prompt)])],
            temperature=0.7
        )
        response = await self._llm.generate_content(request=request)

        # DEBUG: Log response
        debug_logger.log_response(
            agent_name="consolidation_v2",
            response=response.text,
            metadata={"user_id": user_id[:8] if user_id else "unknown", "model": self.model_name}
        )

        return self._parse_consolidation_results(response.text)
    
    def _parse_consolidation_results(self, response_text: str) -> Dict[str, Any]:
        """Parse consolidation results from LLM JSON response (v2 format)."""
        try:
            # Try markdown block first
            json_match = re.search(
                r"```json\s*(\{.*?\})\s*```",
                response_text,
                re.DOTALL
            )
            
            if json_match:
                return json.loads(json_match.group(1))
            
            # Try plain JSON (LLM may return without markdown wrapper)
            return json.loads(response_text.strip())
            
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️  No JSON found in LLM response")
            logger.error(f"Failed to parse JSON: {e}")
            return {}
    
    def _sanitize_ids(self, items: List[Dict]) -> List[Dict]:
        """Ensure ID uniqueness within batch."""
        seen_ids = set()
        sanitized = []
        
        for item in items:
            # Handle case where LLM returns strings instead of dicts
            if isinstance(item, str):
                logger.warning(f"LLM returned string instead of dict: '{item}' - skipping")
                continue
                
            if not isinstance(item, dict):
                logger.warning(f"LLM returned invalid type: {type(item)} - skipping")
                continue
            
            original_id = item.get('id')
            if not original_id:
                continue
            
            new_id = original_id
            suffix = 0
            
            while new_id in seen_ids:
                suffix += 1
                new_id = f"{original_id}_{chr(96 + suffix)}"
            
            if new_id != original_id:
                logger.warning(f"Sanitized duplicate ID: '{original_id}' → '{new_id}'")
                item['id'] = new_id
            
            seen_ids.add(new_id)
            sanitized.append(item)
        
        return sanitized

    def _get_alternative_agents(self) -> List[str]:
        """Suggest alternative agents."""
        return []
