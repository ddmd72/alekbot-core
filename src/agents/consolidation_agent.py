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
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..domain.entities import FactEntity, FactType
from ..domain.request_context import get_effective_account_id, get_current_user_id
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_management_port import FactManagementPort
from ..ports.llm_port import LLMPort, LLMResponse, ToolCall, Message, MessagePart, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..ports.llm_port import AgentExecutionContext
from ..ports.fact_write_port import FactWritePort
from ..infrastructure.agent_config import CONSOLIDATION


@dataclass
class ToolResponse:
    """Internal container for fact management tool results."""
    name: str
    result_str: str


class _TrackingFactManagement(FactManagementPort):
    """
    Pass-through wrapper over FactManagementPort.
    Records (fact_id, content) for every written fact (CREATE / UPDATE / MERGE).
    Used by Stage 1 to seed the Stage 2 cluster review.
    """

    def __init__(self, real: FactManagementPort) -> None:
        self._real = real
        self.changed: List[Tuple[str, str]] = []  # (fact_id, content)

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return await self._real.search_existing_facts(
            keywords=keywords,
            primary_query=primary_query,
            alternative_query=alternative_query,
            limit=limit,
        )

    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._real.create_fact(content=content, metadata=metadata)
        if fid := result.get("fact_id"):
            self.changed.append((fid, content))
        return result

    async def update_fact(self, fact_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._real.update_fact(fact_id=fact_id, updates=updates)
        if content := updates.get("content"):
            self.changed.append((fact_id, content))
        return result

    async def merge_facts(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        result = await self._real.merge_facts(
            fact_ids=fact_ids, merged_content=merged_content, metadata=metadata
        )
        if fid := result.get("new_fact_id"):
            self.changed.append((fid, merged_content))
        return result

    async def discard_candidate(self, reason: str) -> Dict[str, Any]:
        return await self._real.discard_candidate(reason=reason)


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
    
    MAX_CONSOLIDATION_TURNS = CONSOLIDATION.max_turns
    TEMPERATURE = CONSOLIDATION.temperature
    MAX_TOKENS = CONSOLIDATION.max_tokens
    THINKING_EFFORT = CONSOLIDATION.thinking_effort
    INLINE_CLUSTER_REVIEW = CONSOLIDATION.inline_cluster_review

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
        facts_limit: int = CONSOLIDATION.facts_limit,
        principles_limit: int = CONSOLIDATION.principles_limit,
        indexed_email_repo: Optional[IndexedEmailRepository] = None,
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
        self._indexed_email_repo = indexed_email_repo

        logger.info(
            f"👨‍🏫 ConsolidationAgent initialized "
            f"(version={prompt_version}, model={self.model_name}, "
            f"facts_limit={facts_limit}, principles_limit={principles_limit})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.DELEGATE:
            return False
        task = message.payload.get("task", "").lower()
        return task in [
            "consolidate", "consolidate_cluster", "consolidate_email", "consolidate_full",
            "synthesize", "librarian",  # legacy aliases → consolidate
        ]
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute consolidation process.

        Dispatches by payload["task"]:
          consolidate          → Stage 1 (+ optional inline Stage 2)
          consolidate_cluster  → Stage 2 cluster review only
          consolidate_email    → email triage only  [not yet implemented]
          consolidate_full     → Stage 1 + Stage 2 + email  [not yet implemented]
          synthesize/librarian → legacy aliases for consolidate
        """
        user_id = message.context.get("user_id")

        if not user_id:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Missing user_id in context"
            )

        self._on_agent_start(user_id[:8] if user_id else "")
        start_time = time.time()

        try:
            payload = message.payload
            task = payload.get("task", "consolidate").lower()

            # Resolve account_id from RequestContext
            account_id = get_effective_account_id()
            if not account_id:
                raise ValueError(
                    "Missing account_id in RequestContext. "
                    "ConversationHandler must set RequestContext before calling consolidation."
                )

            # Load biographical context (shared across all task types)
            bio_context_raw = payload.get("biographical_context")
            if not bio_context_raw:
                try:
                    logger.info("📚 Context missing in payload, loading from cache...")
                    bio_context_raw = await self._repo.get_biographical_context_cached(limit=100)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to load biographical context from cache: {e}")
                    bio_context_raw = []

            # --- Dispatch ---
            if task in ("consolidate", "synthesize", "librarian"):
                return await self._handle_consolidate(
                    message, user_id, account_id, bio_context_raw, start_time
                )
            if task == "consolidate_cluster":
                return await self._handle_consolidate_cluster(
                    message, user_id, account_id, bio_context_raw, start_time
                )
            if task == "consolidate_email":
                return await self._handle_consolidate_email(
                    message, user_id, account_id, bio_context_raw, start_time
                )
            if task == "consolidate_full":
                return await self._handle_consolidate_full(
                    message, user_id, account_id, bio_context_raw, start_time
                )

            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Unknown task: {task}",
            )

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Consolidation failed: {str(e)}"
            )

    async def _handle_consolidate(
        self,
        message: AgentMessage,
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict],
        start_time: float,
    ) -> AgentResponse:
        """
        Intent: consolidate — Stage 1 conversation processing + optional inline Stage 2.
        Payload: {"messages": [...]}  (list of conversation turns)
        """
        try:
            payload = message.payload
            messages = payload.get("messages", [])
            observations = payload.get("observations", [])

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
            saved_count, skipped_count, _ = await self._fact_write_service.add_facts_batch(
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
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Consolidation failed: {str(e)}"
            )

    async def _handle_consolidate_cluster(
        self,
        message: AgentMessage,
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict],
        start_time: float,
    ) -> AgentResponse:
        """
        Intent: consolidate_cluster — Stage 2 cluster review only.
        Payload: {"cluster": [...]}  — explicit list of fact dicts
              OR {"limit": int}      — auto-fetch top-K by word count [not yet implemented]
        """
        try:
            cluster = message.payload.get("cluster")
            if cluster is None:
                limit = message.payload.get("limit", 10)
                facts = await self._repo.get_longest_facts(account_id, limit=limit)
                cluster = [
                    {"fact_id": f.id, "content": f.text, "similarity": 0.0}
                    for f in facts
                ]
                logger.info(
                    f"👨‍🏫 [ConsolidationAgent] consolidate_cluster auto-fetch: "
                    f"{len(cluster)} facts (limit={limit})"
                )
                if not cluster:
                    return AgentResponse.success(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        result={"operations": 0, "facts_affected": 0, "message": "No facts to review"},
                        confidence=1.0,
                        metadata={"total_duration_ms": int((time.time() - start_time) * 1000)},
                    )

            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="consolidation",
                user_id=user_id,
                account_id=account_id,
                routing_metadata=None,
                biographical_facts=bio_context_raw or [],
                conversation_history=[],
            )


            ops = await self._run_consolidation_loop(
                user_message_text=self._build_cluster_message(cluster),
                system_prompt=system_prompt,
                user_id=user_id,
                account_id=account_id,
            )

            facts_affected = sum(1 for op in ops if op.get("action") in ("CREATE", "UPDATE", "MERGE"))
            await self._repo.refresh_biographical_context_cache(
                owner_id=account_id,
                facts_limit=self.facts_limit,
                principles_limit=self.principles_limit,
            )
            if self.prompt_builder:
                self.prompt_builder.invalidate_biographical_cache(account_id)

            total_duration = time.time() - start_time
            logger.info(
                f"✅ [ConsolidationAgent] consolidate_cluster done: {len(ops)} ops, "
                f"{facts_affected} facts affected in {total_duration:.1f}s"
            )
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={"operations": len(ops), "facts_affected": facts_affected},
                confidence=1.0,
                metadata={"total_duration_ms": int(total_duration * 1000), "operations": ops},
            )

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"consolidate_cluster failed: {str(e)}",
            )

    async def _handle_consolidate_email(
        self,
        message: AgentMessage,
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict],
        start_time: float,
    ) -> AgentResponse:
        """
        Intent: consolidate_email — Stage 3 email triage only. No inline cluster review.
        Payload: {"number_of_batches": int, "batch_size": int}  (both optional, use config defaults)

        Fetches unconsolidated email facts from IndexedEmailRepository, formats them as
        candidates, runs ConsolidationAgent LLM loop (Stage 1 only), marks as consolidated.
        Mirrors the old _run_email_triage_pass logic from consolidation_handler.py.
        """
        if self._indexed_email_repo is None:
            logger.info("📧 [ConsolidationAgent] No indexed_email_repo — email triage skipped")
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={"batches_processed": 0, "facts_affected": 0, "message": "No email repo"},
                confidence=1.0,
                metadata={"total_duration_ms": 0},
            )

        n_passes = message.payload.get("number_of_batches", CONSOLIDATION.email_triage_passes)
        batch_size = message.payload.get("batch_size", CONSOLIDATION.email_triage_batch_size)

        # Build system prompt once — bio context doesn't change between batches
        system_prompt = await self.prompt_builder.build_for_agent(
            agent_type="consolidation",
            user_id=user_id,
            account_id=account_id,
            routing_metadata=None,
            biographical_facts=bio_context_raw or [],
            conversation_history=[],
        )


        total_facts_affected = 0
        batches_processed = 0

        for pass_num in range(1, n_passes + 1):
            emails = await self._indexed_email_repo.get_unconsolidated_batch(
                user_id, limit=batch_size
            )
            if not emails:
                logger.info(
                    f"📧 [ConsolidationAgent] Email triage pass {pass_num}: no unconsolidated emails"
                )
                break

            logger.info(
                f"📧 [ConsolidationAgent] Email triage pass {pass_num}/{n_passes}: "
                f"{len(emails)} candidates"
            )

            candidates_text = self._format_email_candidates(emails)
            system_alert = (
                "[system_alert] The system has scanned the user's email inbox on their behalf "
                "and selected candidates for inclusion in the fact database. "
                "The selection contains noise. Evaluate the incoming data and process it "
                "according to your algorithm.\n\n"
                f"Candidates:\n{candidates_text}"
            )

            ops = await self._run_consolidation_loop(
                user_message_text=system_alert,
                system_prompt=system_prompt,
                user_id=user_id,
                account_id=account_id,
            )

            now = datetime.now(timezone.utc)
            email_ids = [e.email_id for e in emails]
            await self._indexed_email_repo.mark_consolidated(user_id, email_ids, now)

            facts_affected = sum(1 for op in ops if op.get("action") in ("CREATE", "UPDATE", "MERGE"))
            total_facts_affected += facts_affected
            batches_processed += 1

            logger.info(
                f"✅ [ConsolidationAgent] Email triage pass {pass_num} done: "
                f"{len(ops)} ops, {facts_affected} facts affected"
            )

            if len(emails) < batch_size:
                break  # no more unconsolidated emails

        if batches_processed > 0:
            await self._repo.refresh_biographical_context_cache(
                owner_id=account_id,
                facts_limit=self.facts_limit,
                principles_limit=self.principles_limit,
            )
            if self.prompt_builder:
                self.prompt_builder.invalidate_biographical_cache(account_id)

        total_duration = time.time() - start_time
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"batches_processed": batches_processed, "facts_affected": total_facts_affected},
            confidence=1.0,
            metadata={"total_duration_ms": int(total_duration * 1000)},
        )

    async def _handle_consolidate_full(
        self,
        message: AgentMessage,
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict],
        start_time: float,
    ) -> AgentResponse:
        """
        Intent: consolidate_full — Stage 1 (+ optional inline Stage 2) → Stage 3 email triage.
        Payload: same as consolidate (messages list for Stage 1).

        Stage 1/2 failure aborts the full pipeline — email is not attempted.
        """
        stage1_response = await self._handle_consolidate(
            message, user_id, account_id, bio_context_raw, start_time
        )
        if stage1_response.status != AgentStatus.SUCCESS:
            return stage1_response

        email_start = time.time()
        email_message = AgentMessage(
            task_id=message.task_id,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            payload={"task": "consolidate_email"},
            context=message.context,
        )
        email_response = await self._handle_consolidate_email(
            email_message, user_id, account_id, bio_context_raw, email_start
        )

        stage1_result = stage1_response.result or {}
        email_result = email_response.result or {}
        total_duration = time.time() - start_time
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={
                "stage1_operations": stage1_result.get("operations", stage1_result.get("new_facts", 0)),
                "stage1_facts_affected": stage1_result.get("facts_affected", 0),
                "email_batches": email_result.get("batches_processed", 0),
                "email_facts_affected": email_result.get("facts_affected", 0),
            },
            confidence=1.0,
            metadata={"total_duration_ms": int(total_duration * 1000)},
        )

    @staticmethod
    def _format_email_candidates(emails) -> str:
        """Format indexed email facts as numbered JSON candidates for the LLM."""
        import json as _json
        lines = []
        for i, email in enumerate(emails, 1):
            candidate = {
                "email_id": email.email_id,
                "fact": email.text,
                "category": email.category,
                "tags": email.tags,
                "date": email.email_date.strftime("%Y-%m-%d"),
                "from": email.from_address,
                "subject": email.subject,
            }
            if email.attachments:
                candidate["attachments"] = email.attachments
            if email.metadata:
                candidate["metadata"] = email.metadata
            lines.append(f"{i}. {_json.dumps(candidate, ensure_ascii=False)}")
        return "\n".join(lines)

    async def _execute_deliberate_process_v3(
        self,
        messages: List[Dict],
        user_id: str,
        account_id: str,
        bio_context_raw: List[Dict]
    ) -> Dict[str, Any]:
        """
        Two-stage deliberate fact management process (v3).

        Stage 1: process conversation → create/update/merge facts.
        Stage 2: cluster review — for each written fact, semantic search → merge cluster
                 → second LLM pass with RFC cluster prompt (no co-location exception).

        Returns:
            Dict with "operations" list (Stage 1 ops + Stage 2 ops combined)
        """
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

        biographical_facts = bio_context_raw or []

        # Stage 1: full system prompt with conversation history
        system_prompt = await self.prompt_builder.build_for_agent(
            agent_type="consolidation",
            user_id=user_id,
            account_id=account_id,
            routing_metadata=None,
            biographical_facts=biographical_facts,
            conversation_history=conversation_history
        )


        # Install tracker to capture written (fact_id, content) pairs
        tracker = _TrackingFactManagement(self._fact_management)
        self._fact_management = tracker
        try:
            ops1 = await self._run_consolidation_loop(
                user_message_text="Begin deliberate consolidation.",
                system_prompt=system_prompt,
                user_id=user_id,
                account_id=account_id,
            )
        finally:
            self._fact_management = tracker._real

        logger.info(
            f"👨‍🏫 [ConsolidationAgent] Stage 1 done: {len(ops1)} ops, "
            f"{len(tracker.changed)} facts written"
        )

        # Stage 2: inline cluster review (skip if disabled or nothing was written)
        if not self.INLINE_CLUSTER_REVIEW or not tracker.changed:
            return {"operations": ops1}

        cluster = await self._build_review_cluster(tracker.changed, account_id)
        if not cluster:
            logger.info("👨‍🏫 [ConsolidationAgent] Stage 2 skipped — empty cluster")
            return {"operations": ops1}

        logger.info(
            f"👨‍🏫 [ConsolidationAgent] Stage 2: reviewing cluster of {len(cluster)} facts"
        )

        # Stage 2 system prompt: no conversation history (cluster message provides full context)
        system_prompt_2 = await self.prompt_builder.build_for_agent(
            agent_type="consolidation",
            user_id=user_id,
            account_id=account_id,
            routing_metadata=None,
            biographical_facts=biographical_facts,
            conversation_history=[],
        )


        ops2 = await self._run_consolidation_loop(
            user_message_text=self._build_cluster_message(cluster),
            system_prompt=system_prompt_2,
            user_id=user_id,
            account_id=account_id,
        )

        logger.info(f"👨‍🏫 [ConsolidationAgent] Stage 2 done: {len(ops2)} ops")
        return {"operations": ops1 + ops2}

    async def _run_consolidation_loop(
        self,
        user_message_text: str,
        system_prompt: str,
        user_id: str,
        account_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Single multi-turn LLM loop for one consolidation pass.
        Returns parsed operations list from the final report.
        """
        history: List[Message] = [
            Message(role="user", parts=[MessagePart(text=user_message_text)])
        ]

        for turn in range(self.MAX_CONSOLIDATION_TURNS):
            logger.info(
                f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1}/{self.MAX_CONSOLIDATION_TURNS}"
            )

            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=history,
                tools=self._get_tool_declarations(),
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                timeout=500,
                thinking=self.THINKING_EFFORT or None,
            )

            response: LLMResponse = await self._call_llm(request, turn=turn + 1)

            if not response.tool_calls:
                logger.info(f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1} — final report received")
                return self._parse_operations_report(response.text or "")

            logger.info(
                f"👨‍🏫 [ConsolidationAgent] Turn {turn + 1} — executing {len(response.tool_calls)} tools"
            )

            if response.raw_content:
                history.append(Message(role="model", parts=[], raw_content=response.raw_content))
            else:
                history.append(Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls]
                ))

            # Mirror RouterAgent pattern: touch Firestore between LLM and find_nearest
            # to keep Cloud Run CPU active and unblock gRPC calls.
            await self._repo.get_biographical_context_cached(owner_id=account_id, limit=100)

            tool_responses = await self._execute_fact_management_tools(
                tool_calls=response.tool_calls,
                user_id=user_id,
                account_id=account_id,
            )

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

        logger.warning("⚠️ [ConsolidationAgent] Max turns reached without final report")
        return []

    async def _build_review_cluster(
        self,
        changed: List[Tuple[str, str]],
        account_id: str,
    ) -> List[Dict[str, Any]]:
        """
        For each written (fact_id, content) from Stage 1 → semantic search → merge,
        dedup by fact_id, sort by content length DESC, limit 30.
        Seeds Stage 2 cluster review.
        """
        async def _search_one(content: str) -> List[Dict]:
            keywords = [w for w in content.split()[:10] if len(w) > 3]
            return await self._fact_management.search_existing_facts(
                keywords=keywords,
                primary_query=content,
                alternative_query="",
                limit=20,
            )

        contents = [c for _, c in changed if c]
        if not contents:
            return []

        all_results: List[List[Dict]] = list(
            await asyncio.gather(*[_search_one(c) for c in contents])
        )

        seen: Dict[str, Any] = {}
        for results in all_results:
            for fact in results:
                fid = fact.get("fact_id")
                if not fid:
                    continue
                if fid not in seen or (fact.get("similarity") or 0) > (seen[fid].get("similarity") or 0):
                    seen[fid] = fact

        merged = sorted(
            seen.values(),
            key=lambda f: len((f.get("content") or "").split()),
            reverse=True,
        )
        result = merged[:30]
        logger.info(
            f"👨‍🏫 [ConsolidationAgent] Cluster: {len(result)} unique facts "
            f"(from {sum(len(r) for r in all_results)} results across {len(contents)} searches)"
        )
        return result

    @staticmethod
    def _build_cluster_message(cluster: List[Dict[str, Any]]) -> str:
        """RFC-validated cluster review user message (verbatim from Stage 2 POC)."""
        alert = (
            "SYSTEM MAINTENANCE — FACT CLUSTER REVIEW\n\n"
            "The system has flagged the following cluster of facts for quality review.\n"
            "This cluster may contain: repeated or overlapping facts (these must be merged),\n"
            "facts that span multiple distinct concepts (these must be decomposed, with the\n"
            "original superseded), mutually inconsistent facts, or facts that have grown\n"
            "too large to serve as atomic memory units.\n\n"
            "Review and refactor this cluster according to your consolidation rules.\n"
            "When creating new facts, ensure they do not duplicate information already\n"
            "present in other facts in this cluster.\n\n"
            "Hard limit: no fact may exceed 40 words. Every fact in this cluster that\n"
            "exceeds 40 words must be either rephrased to fit within 40 words, or\n"
            "decomposed into atomic facts each under 40 words. Co-location is not a\n"
            "valid justification for exceeding this limit.\n\n"
            "Important: do not lose specific numeric values, dates, or amounts —\n"
            "they are critical for long-term memory accuracy."
        )
        lines = [alert, "", ""]
        for i, fact in enumerate(cluster, 1):
            obj = {
                "fact_id": fact.get("fact_id"),
                "content": fact.get("content"),
                "similarity": round(fact["similarity"], 3) if fact.get("similarity") is not None else None,
            }
            lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
        return "\n".join(lines)
    
    async def _execute_fact_management_tools(
        self,
        tool_calls: List[ToolCall],
        user_id: str,
        account_id: str
    ) -> List[ToolResponse]:
        """Execute fact management tools via FactManagementPort.

        All tool calls within a single LLM turn are dispatched concurrently via
        asyncio.gather. search_existing_facts calls are always independent (each
        searches for a different candidate fact). Write tools (create/update/merge)
        reference fact_ids from prior search results, never each other.
        asyncio.gather preserves order — tool_responses[i] matches tool_calls[i].
        """

        async def _execute_one(tool_call: ToolCall) -> ToolResponse:
            logger.info(f"🔧 [ConsolidationAgent] Executing tool: {tool_call.name}")
            try:
                if tool_call.name == "search_existing_facts":
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
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    )

                elif tool_call.name == "create_fact":
                    fact_attributes = tool_call.args.get("fact_attributes", {})
                    fact_attributes["account_id"] = account_id
                    fact_attributes["user_id"] = user_id

                    result = await self._fact_management.create_fact(
                        content=tool_call.args.get("content", ""),
                        metadata=fact_attributes
                    )
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    )

                elif tool_call.name == "update_fact":
                    result = await self._fact_management.update_fact(
                        fact_id=tool_call.args.get("fact_id", ""),
                        updates=tool_call.args.get("updates", {})
                    )
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    )

                elif tool_call.name == "merge_facts":
                    fact_attributes = tool_call.args.get("fact_attributes", {})
                    fact_attributes["account_id"] = account_id
                    fact_attributes["user_id"] = user_id

                    result = await self._fact_management.merge_facts(
                        fact_ids=tool_call.args.get("fact_ids", []),
                        merged_content=tool_call.args.get("merged_content", ""),
                        metadata=fact_attributes
                    )
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    )

                elif tool_call.name == "count_words":
                    text = tool_call.args.get("text", "")
                    word_count = len(text.split())
                    result = {
                        "word_count": word_count,
                        "limit": 40,
                        "within_limit": word_count <= 40,
                        "excess": max(0, word_count - 40),
                    }
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps(result, ensure_ascii=False)
                    )

                else:
                    logger.warning(f"⚠️ Unknown tool: {tool_call.name}")
                    return ToolResponse(
                        name=tool_call.name,
                        result_str=json.dumps({"error": f"Unknown tool: {tool_call.name}"})
                    )

            except Exception as e:
                logger.error(f"❌ Tool {tool_call.name} failed: {e}", exc_info=True)
                return ToolResponse(
                    name=tool_call.name,
                    result_str=json.dumps({"error": str(e)}, ensure_ascii=False)
                )

        if len(tool_calls) > 1:
            logger.info(
                f"👨‍🏫 [ConsolidationAgent] Dispatching {len(tool_calls)} tools in parallel"
            )
            return list(await asyncio.gather(*[_execute_one(tc) for tc in tool_calls]))

        return [await _execute_one(tool_calls[0])]
    
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
                "name": "count_words",
                "description": "Count words in a text string. Call this BEFORE create_fact or update_fact to verify content is ≤40 words. Returns exact word_count, within_limit flag, and excess words if any.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The text to count words in (e.g. the planned content field value)"
                        }
                    },
                    "required": ["text"]
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

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction="You are Life Chronicler. Return only valid JSON.",
            messages=[Message(role="user", parts=[MessagePart(text=prompt)])],
            temperature=self.TEMPERATURE,
            timeout=500,
        )
        response = await self._call_llm(request)

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
