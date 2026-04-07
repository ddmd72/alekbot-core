from __future__ import annotations

import time
import asyncio
import re
from datetime import datetime, timezone
from typing import Dict, Optional, List, TYPE_CHECKING
from ..ports.repository import FactRepository
from ..ports.llm_port import ProviderCapabilities
from ..domain.agent import RoutingMetadata
from ..domain.tone import UserTone
from ..domain.user import UserBotConfig
from ..utils.logger import logger
from ..utils.timer import log_execution_time

from ..domain.prompt import (
    ANONYMOUS_USER_ID,
    ANONYMOUS_ACCOUNT_ID
)
from ..ports.prompt_builder_port import PromptBuilderPort

if TYPE_CHECKING:
    # Prompt Design System v3 Integration (Phase 4)
    # RFC: docs/10_rfcs/PROMPT_DESIGN_SYSTEM_RFC.md
    from ..services.prompt_v3.prompt_assembly_service import PromptAssemblyService


class PromptBuilder(PromptBuilderPort):
    """
    Provider-agnostic prompt builder service.

    Uses PromptAssemblyService (v3) to assemble prompts from tokens and blueprints.
    """

    def __init__(
        self,
        repo: FactRepository,
        cache_ttl: int = 3600,
        assembly_service: Optional['PromptAssemblyService'] = None
    ):
        """
        Initialize PromptBuilder.

        Args:
            repo: FactRepository for fetching biographical facts
            cache_ttl: Time-to-live for cached components (legacy)
            assembly_service: PromptAssemblyService for token-based assembly
        """
        self.repo = repo
        self.cache_ttl = cache_ttl
        self.assembly_service = assembly_service
        # Cache format: {component_key: (content, timestamp)}
        self._component_cache: Dict[str, tuple] = {}

    @log_execution_time
    async def preload_components(self) -> None:
        """
        Preloads all system components from Firestore in a single batch query.
        Significantly reduces cold start time by avoiding multiple round-trips.
        """
        logger.info("📋 [PromptBuilder] Preloading all system components...")
        start_time = time.time()
        
        # Fetch all active system facts in one query
        system_facts = await self.repo.get_active_facts("SYSTEM")
        
        current_time = time.time()
        for fact in system_facts:
            cache_key = f"prompt_component:{fact.lineage_id}"
            self._component_cache[cache_key] = (fact.text, current_time)
            
        logger.info(f"📋 [PromptBuilder] Preloaded {len(system_facts)} components in {time.time() - start_time:.2f}s")

    def merge_enriched_context_with_biographical(
        self,
        enriched_context: Optional[Dict],
        cached_biographical: Optional[List[Dict]] = None
    ) -> List[Dict]:
        """
        Merge Router enriched facts with cached biographical facts.
        
        Router enrichment is already deduplicated against biographical
        by SearchEnrichmentService, so we just need format conversion.
        
        Args:
            enriched_context: Router enriched context dict with "facts" key
            cached_biographical: Cached biographical facts (optional, for merge)
        
        Returns:
            List of biographical facts in standard format
        """
        biographical_facts = list(cached_biographical) if cached_biographical else []
        
        if not enriched_context or not enriched_context.get("facts"):
            return biographical_facts
        
        # Convert enriched facts to biographical format
        for fact in enriched_context.get("facts", []):
            biographical_facts.append({
                "text": fact.get("content", ""),
                "type": "SEMANTIC",  # Mark as semantic search result
                "source": fact.get("source", "router_enrichment"),
                "tags": ["semantic_lens"]  # For future filtering if needed
            })
        
        logger.debug(
            f"📋 [PromptBuilder] Merged enriched context: "
            f"{len(enriched_context.get('facts', []))} semantic "
            f"+ {len(cached_biographical) if cached_biographical else 0} cached "
            f"= {len(biographical_facts)} total"
        )
        
        return biographical_facts

    @log_execution_time
    async def build_for_agent(
        self,
        agent_type: str,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        routing_metadata: Optional[RoutingMetadata] = None,
        capabilities: Optional[ProviderCapabilities] = None,
        biographical_facts: Optional[List[Dict]] = None,
        conversation_history: Optional[List[dict]] = None,
        include_biographical: bool = True,
        kb_preamble: bool = False,
        agent_notes: Optional[List[dict]] = None,
        extra_static_blocks: Optional[List[str]] = None,
        include_datetime: bool = False,
    ) -> str:
        """
        Build complete system prompt for agent using PromptAssemblyService.

        Args:
            agent_type: "quick" or "smart"
            user_id: Optional user ID
            account_id: Optional account ID
            routing_metadata: Optional routing metadata
            semantic_context: Optional semantic context string
            capabilities: Provider capabilities
            biographical_facts: Optional pre-fetched biographical facts (override)
            conversation_history: Optional conversation history for runtime injection
            include_biographical: Whether to load biographical facts from Firestore.
                Set False for agents that don't need personal context (e.g. router).

        Returns:
            Fully formatted system prompt string
        """
        if not self.assembly_service:
            raise ValueError("assembly_service is required for build_for_agent()")

        # Fetch biographical facts unless explicitly disabled or already provided
        if not include_biographical:
            biographical_facts = []
        elif biographical_facts is None:
            biographical_facts = []

            # Facts belong to account (OAuth Multi-Tenant)
            if account_id:
                try:
                    biographical_facts = await self.repo.get_biographical_context_cached(account_id)
                except Exception as e:
                    logger.warning(f"Failed to fetch biographical facts: {e}")
            elif user_id:
                # Log warning but do not fallback to user_id for facts (strict separation)
                logger.warning(
                    f"PromptBuilder: Missing account_id for user {user_id}, skipping biographical facts"
                )

        if conversation_history is None:
            conversation_history = []

        # Split biographical_facts: static (long-term) vs query-specific (tagged semantic_lens).
        # Agents call merge_enriched_context_with_biographical() before build_for_agent(), which
        # tags router-enriched facts with "semantic_lens". The assembly service never sees that tag.
        static_bio = [
            f for f in biographical_facts
            if "semantic_lens" not in (f.get("tags", []) if isinstance(f, dict) else [])
        ]
        qs_facts = [
            f for f in biographical_facts
            if "semantic_lens" in (f.get("tags", []) if isinstance(f, dict) else [])
        ]
        if qs_facts:
            qs_lines = ["**Query-Specific Context:**"]
            for fact in qs_facts:
                text = (fact.get("text") or "").strip()
                if text:
                    qs_lines.append(f"- {text}")
            query_specific_context: Optional[str] = "\n".join(qs_lines)
        else:
            query_specific_context = None

        _cfg = getattr(self, "config", None)
        user_timezone = _cfg.timezone if _cfg else "UTC"
        user_location = getattr(_cfg, "location", None) if _cfg else None

        return await self.assembly_service.assemble(
            agent_type=agent_type,
            user_id=user_id or ANONYMOUS_USER_ID,
            account_id=account_id or ANONYMOUS_ACCOUNT_ID,
            biographical_facts=static_bio,
            conversation_history=conversation_history,
            query_specific_context=query_specific_context,
            kb_preamble=kb_preamble,
            agent_notes=agent_notes,
            user_timezone=user_timezone,
            user_location=user_location,
            extra_static_blocks=extra_static_blocks,
            include_datetime=include_datetime,
        )

    async def _get_biographical_component(self, user_id: str) -> str:
        """
        Get biographical context component (cached until explicit invalidation).

        Uses SAME cache mechanism as other components.
        Difference: invalidated by ConsolidationAgent, not by TTL.

        Args:
            user_id: User identifier

        Returns:
            Formatted biographical context string
        """
        cache_key = f"prompt_component:biographical_context_{user_id}"
        current_time = time.time()

        # Check cache validity (No TTL check for biographical context, stays fresh until consolidation)
        if cache_key in self._component_cache:
            content, timestamp = self._component_cache[cache_key]
            logger.debug(f"📋 [PromptBuilder] Cache hit for biographical_context (user={user_id[:8]})")
            return content

        # Cache miss - fetch from repository
        logger.debug(f"📋 [PromptBuilder] Fetching biographical_context for user {user_id[:8]}")

        try:
            bio_facts = await self.repo.get_biographical_context_cached(
                owner_id=user_id,
                limit=100
            )

            # Format for Groovy-style prompt
            content = self._format_biographical_facts(bio_facts)

            # Store in cache
            self._component_cache[cache_key] = (content, current_time)

            return content

        except Exception as e:
            logger.warning(f"⚠️ Failed to load biographical context: {e}")
            return "// Biographical context unavailable"

    def _format_biographical_facts(self, bio_facts: List[dict]) -> str:
        """
        Format biographical facts for Groovy prompt injection.
        """
        if not bio_facts:
            return "// No biographical data available yet."

        lines = []
        for fact in bio_facts:
            text = fact.get('text', '')
            if text:
                lines.append(f"- {text}")

        return "\n".join(lines)

    def _get_static_rules(self) -> str:
        """
        Get static Slack formatting rules (doesn't change, so no caching needed).
        """
        return """
@critical rule Slack_Formatting_Protocol() {
  instruction: "Your responses will be displayed in Slack, which uses a specific 'mrkdwn' format. You MUST adhere to it strictly."
  instruction: "For bold text, you MUST use single asterisks: *bold text*."
  instruction: "For italic text, you MUST use underscores: _italic text*."
  instruction: "For lists, you MUST use bullet points with an asterisk and a space: * List item."
  instruction: "Do NOT use standard Markdown like '**bold**' or numbered lists ('1. ...'), as they will not render correctly."
}
"""

    def _build_lens_instructions(self, lens: Dict) -> str:
        """
        Build lens-specific instructions (for Milestone 5).

        Args:
            lens: Lens configuration dict

        Returns:
            Formatted lens instructions
        """
        lens_name = lens.get('name', 'Unknown')
        weights = lens.get('weights', {})

        return f"""
@context Lens_Active {{
  name: "{lens_name}"
  instruction: "Prioritize {lens_name.lower()} domain knowledge."
  search_weights: {{
    vector: {weights.get('lambda_vector', 1.0)},
    recency: {weights.get('lambda_recency', 1.0)}
  }}
}}
"""

    def invalidate_cache(self, component_key: Optional[str] = None) -> None:
        """
        Manually invalidate cache entries.

        Args:
            component_key: Specific component to invalidate, or None for all
        """
        if component_key:
            cache_key = f"prompt_component:{component_key}"
            if cache_key in self._component_cache:
                del self._component_cache[cache_key]
                logger.info(f"📋 [PromptBuilder] Invalidated cache for {component_key}")
        else:
            # Clear all cache
            self._component_cache.clear()
            logger.info("📋 [PromptBuilder] Invalidated entire cache")

    def invalidate_biographical_cache(self, user_id: str) -> None:
        """
        Invalidate biographical cache for specific user.
        Called by ConsolidationAgent after successful consolidation.

        Args:
            user_id: User identifier
        """
        cache_key = f"prompt_component:biographical_context_{user_id}"
        if cache_key in self._component_cache:
            del self._component_cache[cache_key]
            logger.info(f"📋 [PromptBuilder] Invalidated biographical cache for user {user_id[:8]}")

    def get_cache_stats(self) -> Dict:
        """
        Get cache statistics for monitoring.

        Returns:
            Dict with cache stats
        """
        total_entries = len(self._component_cache)
        current_time = time.time()

        expired_count = 0
        for content, timestamp in self._component_cache.values():
            if (current_time - timestamp) >= self.cache_ttl:
                expired_count += 1

        return {
            'total_entries': total_entries,
            'expired_entries': expired_count,
            'cache_ttl_seconds': self.cache_ttl,
            'cache_hit_ratio_estimate': 1.0 - (expired_count / max(total_entries, 1))
        }


class UserPromptBuilder(PromptBuilder):
    """
    Extends PromptBuilder to support per-user prompt component overrides.

    SESSION_26: Now supports component_service for 3-level priority resolution.

    Lookup order (NEW with component_service):
    1. USER level (owner_type=USER, owner_value=user_id)
    2. AGENT level (owner_type=AGENT, owner_value=agent_type)
    3. SYSTEM level (owner_type=SYSTEM)

    Legacy lookup order (without component_service):
    1. User's custom component (if defined in UserBotConfig)
    2. Default SYSTEM component
    """

    def __init__(
        self,
        repo: FactRepository,
        user_id: str,
        config: UserBotConfig,
        cache_ttl: int = 3600,
        assembly_service: Optional[object] = None
    ):
        super().__init__(repo, cache_ttl, assembly_service)
        self.user_id = user_id
        self.config = config

    async def _get_component(self, lineage_id: str) -> str:
        """
        Override to check for user-specific components first.
        """
        # Map default lineage_id to custom override (if exists)
        custom_id_map = {
            'kernel': self.config.prompt_preferences.custom_kernel_id,
            'kernel_light': self.config.prompt_preferences.custom_kernel_light_id,
            'examples': self.config.prompt_preferences.custom_examples_id
        }

        custom_id = custom_id_map.get(lineage_id)

        if custom_id:
            # Try to load custom component (owner_id = user_id)
            logger.debug(f"Loading custom {lineage_id} for user {self.user_id}: {custom_id}")
            custom_fact = await self.repo.get_latest_fact_by_lineage(self.user_id, custom_id)
            if custom_fact:
                return custom_fact.text
            else:
                logger.warning(f"Custom component {custom_id} not found for user {self.user_id}, falling back to SYSTEM")

        # Fallback to SYSTEM component (original behavior)
        return await super()._get_component(lineage_id)

