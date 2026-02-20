"""
PromptAssemblyService - Assembles prompts from tokens, blueprints, and profiles.

Part of Prompt Design System v3 (RFC).

PERFORMANCE OPTIMIZATION (2026-02-04):
- Added assembly cache with 24h TTL
- Parallelized repository calls with asyncio.gather()
- See: docs/10_rfcs/PROMPT_ASSEMBLY_CACHING_RFC.md
"""

import logging
import time
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.domain.prompt_v3.security import SecurityPort, TrustZone
from src.domain.prompt_v3.token import TokenId, TokenCategory, TokenClass
from src.domain.prompt_v3.profile_slot import ProfileSlotType
from src.domain.prompt_v3.slot import OwnerType
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
from src.services.prompt_v3.context_formatter import ContextFormatter

logger = logging.getLogger(__name__)


class PromptAssemblyService:
    """Assembles prompts with 3 section types and 4-level resolution.

    Section Types:
        - TOKENIZED: Resolved from token library via classes
        - STATIC: Stored with blueprint (already in template)
        - RUNTIME: Injected at request time with SecurityPort validation

    Resolution Priority: USER > ACCOUNT > AGENT > SYSTEM

    Examples:
        >>> service = PromptAssemblyService(
        ...     token_repo, blueprint_repo, profile_repo, security_port, formatter
        ... )
        >>> prompt = await service.assemble(
        ...     agent_type="smart",
        ...     user_id="user_123",
        ...     account_id="account_456",
        ...     biographical_facts=["Lives in Kyiv", "Software engineer"],
        ...     conversation_history=[{"role": "user", "content": "Hello"}]
        ... )
    """

    def __init__(
        self,
        token_repo: TokenRepository,
        blueprint_repo: BlueprintRepository,
        profile_repo: AgentProfileRepository,
        security_port: SecurityPort,
        formatter: ContextFormatter,
        bio_formatter: BiographicalFactsFormatter,
        cache_ttl: int = 86400  # 24 hours
    ):
        """Initialize PromptAssemblyService.

        Args:
            token_repo: Token storage repository
            blueprint_repo: Blueprint storage repository
            profile_repo: Agent profile storage repository
            security_port: Security validation interface
            formatter: Conversation history formatter
            bio_formatter: Biographical facts formatter
            cache_ttl: Cache time-to-live in seconds (default: 24 hours)
        """
        self.token_repo = token_repo
        self.blueprint_repo = blueprint_repo
        self.profile_repo = profile_repo
        self.security_port = security_port
        self.formatter = formatter
        self.bio_formatter = bio_formatter
        
        # Performance optimization: Assembly cache (RFC: PROMPT_ASSEMBLY_CACHING_RFC.md)
        self._assembled_cache: Dict[str, Tuple[str, float]] = {}  # (prompt, timestamp)
        self._cache_ttl = cache_ttl

    async def assemble(
        self,
        agent_type: str,
        user_id: Optional[str],
        account_id: Optional[str],
        biographical_facts: Optional[List[Dict]] = None,
        conversation_history: Optional[List[dict]] = None
    ) -> str:
        """Full prompt assembly with 3 section types + caching.

        PERFORMANCE OPTIMIZATION:
        - Static template (steps 1-5) is cached with 24h TTL
        - Runtime context (biographical facts, conversation) injected AFTER cache lookup
        - Cache key: (agent_type, account_id, user_id) - no runtime data

        Args:
            agent_type: Agent type (e.g., "smart", "quick")
            user_id: User ID for USER-level overrides (optional)
            account_id: Account ID for ACCOUNT-level overrides (optional)
            biographical_facts: List of biographical facts (RUNTIME validation)
            conversation_history: List of conversation messages (RUNTIME validation)

        Returns:
            Assembled prompt string

        Raises:
            KeyError: If blueprint not found
            ValueError: If validation fails

        Examples:
            >>> prompt = await service.assemble(
            ...     agent_type="smart",
            ...     user_id="user_123",
            ...     account_id="account_456",
            ...     biographical_facts=["Lives in Kyiv"],
            ...     conversation_history=[{"role": "user", "content": "Hi"}]
            ... )
        """
        biographical_facts = biographical_facts or []
        conversation_history = conversation_history or []

        # CACHE CHECK: Build cache key (without runtime data)
        cache_key = self._build_cache_key(agent_type, account_id, user_id)
        cached_template = self._get_from_cache(cache_key)

        if cached_template:
            # CACHE HIT - skip Firestore queries
            logger.info(f"📦 Cache HIT: {cache_key}")
            static_prompt = cached_template
        else:
            # CACHE MISS - assemble from Firestore
            logger.info(f"📦 Cache MISS: {cache_key} - assembling from repositories...")
            
            static_prompt = await self._assemble_static_template(
                agent_type=agent_type,
                account_id=account_id,
                user_id=user_id
            )
            
            # Save to cache
            self._save_to_cache(cache_key, static_prompt)

        # RUNTIME INJECTION (ALWAYS - even on cache hit)
        # This happens AFTER cache because biographical facts and conversation change frequently
        final_prompt = await self._inject_runtime_context(
            static_prompt,
            biographical_facts,
            conversation_history,
            user_id or "anonymous"
        )

        logger.info(f"✅ Assembled prompt: {len(final_prompt)} chars")
        return final_prompt
    
    async def _assemble_static_template(
        self,
        agent_type: str,
        account_id: Optional[str],
        user_id: Optional[str]
    ) -> str:
        """Assemble static prompt template (cacheable part).
        
        This method assembles the prompt WITHOUT runtime context injection.
        Result is cached to avoid repeated Firestore queries.
        
        Steps:
        1. Load blueprint
        2. Resolve profile slots (4-level: USER > ACCOUNT > AGENT > SYSTEM)
        3. Convert slots to token assignments
        4. Fetch tokens and replace {{CLASS_NAME}} placeholders
        5. Remove unreplaced tokens
        
        Args:
            agent_type: Agent type
            account_id: Account ID (optional)
            user_id: User ID (optional)
            
        Returns:
            Static prompt template (without runtime context)
        """
        # 1. Load universal blueprint (v3 uses single universal blueprint)
        blueprint_id = "universal_agent_v1"
        blueprint = await self.blueprint_repo.get(blueprint_id)
        logger.debug(f"Loaded blueprint: {blueprint_id}")

        # 2. Resolve unified class rules (class/category/token with non-overridable)
        resolved_slots = await self._resolve_profile_slots(
            blueprint_id=blueprint_id,
            agent_type=agent_type,
            account_id=account_id,
            user_id=user_id
        )
        logger.debug(f"Resolved {len(resolved_slots)} profile slots")

        # 3. Convert unified slots to assignments dict (class_name → token_id)
        assignments = await self._unified_slots_to_assignments(
            resolved_slots=resolved_slots,
            blueprint=blueprint
        )
        logger.debug(f"Mapped to {len(assignments)} slot assignments")

        # 4. Fetch tokens and replace {{CLASS_NAME}} placeholders
        prompt = blueprint.template
        for slot_name, token_id in assignments.items():
            if f"{{{{{slot_name}}}}}" in prompt:
                token = await self.token_repo.get(token_id)
                prompt = prompt.replace(f"{{{{{slot_name}}}}}", token.content)
                logger.debug(f"Replaced {slot_name} with {token_id}")

        # 5. STATIC sections are already embedded in blueprint.template
        # No additional action needed

        # 6. Remove unreplaced {{TOKENS}} that were not assigned
        prompt = self._remove_unreplaced_tokens(prompt)

        logger.debug(f"Static template assembled: {len(prompt)} chars")
        return prompt

    async def _resolve_profile_slots(
        self,
        blueprint_id: str,
        agent_type: str,
        account_id: Optional[str],
        user_id: Optional[str]
    ) -> List[dict]:
        """Resolve unified slot entries across owner priority.

        PERFORMANCE OPTIMIZATION: Loads all profile levels in PARALLEL with asyncio.gather()
        instead of sequential awaits (4 queries in 50ms instead of 200ms).

        Immutability rule: If non_overridable=true at lower priority level,
        higher priority levels CANNOT override it.

        Returns list of dicts with keys: type, value, non_overridable.
        """
        # Build list of (owner_type, owner_value) pairs
        levels = [("SYSTEM", agent_type)]
        if account_id:
            levels.append(("ACCOUNT", account_id))
        if user_id:
            levels.append(("USER", user_id))

        # Load ALL profiles in PARALLEL (instead of sequential for-loop)
        futures = [
            self.profile_repo.get_profile_slots(
                blueprint_id=blueprint_id,
                owner_type=OwnerType[owner_type],
                owner_value=owner_value
            )
            for owner_type, owner_value in levels
        ]
        
        all_slots_lists = await asyncio.gather(*futures)
        logger.debug(f"Loaded {len(levels)} profile levels in parallel")

        # Merge slots with priority resolution (SYSTEM < ACCOUNT < USER)
        merged: dict[tuple[str, str], dict] = {}
        
        for idx, slots in enumerate(all_slots_lists):
            for slot in slots:
                key = (slot.type.value, slot.value)
                slot_dict = slot.to_dict()
                
                # Immutability enforcement: non_overridable=true cannot be overridden
                if key in merged and merged[key]['non_overridable'] is True:
                    logger.debug(
                        f"Skipping override for {key} - immutable (non_overridable=true)"
                    )
                    continue
                
                merged[key] = slot_dict

        return list(merged.values())

    async def _unified_slots_to_assignments(
        self,
        resolved_slots: List[dict],
        blueprint
    ) -> Dict[str, TokenId]:
        """Convert unified slot entries to class_name → token_id assignments.
        
        PERFORMANCE OPTIMIZATION: Loads all tokens in PARALLEL with asyncio.gather()
        instead of sequential awaits (15+ queries in 50ms instead of 450ms).
        
        Handles:
        - type='token': Direct token assignment
        - type='category': Fetch ALL tokens with that category
        - type='class': Fetch ALL tokens with that class
        - non_overridable=True: Skip
        
        Args:
            resolved_slots: List of dicts with keys: type, value, non_overridable
            blueprint: Blueprint with class schemas
            
        Returns:
            Dict mapping slot_name → token_id
        """
        assignments = {}
        excluded_classes = set()
        
        # PHASE 1: Collect all async queries to execute in parallel
        token_queries = []  # (query_type, value) tuples
        
        for slot_entry in resolved_slots:
            slot_type = slot_entry['type']
            value = slot_entry['value']
            is_non_overridable = slot_entry.get('non_overridable', False)
            
            # Special handling for type='slot' with non_overridable=True (class exclusion)
            if slot_type == 'slot' and is_non_overridable:
                excluded_classes.add(value)
                logger.debug(f"Excluding class: {value} (slot type with non_overridable=true)")
                continue
            
            if slot_type == 'token':
                token_queries.append(('get', TokenId(value)))
            elif slot_type == 'category':
                token_queries.append(('category', TokenCategory(value)))
            elif slot_type == 'class':
                token_queries.append(('class', TokenClass(value)))
            elif slot_type == 'slot':
                logger.debug(f"Slot type 'slot' is reserved: {value}")
        
        # PHASE 2: Execute ALL queries in PARALLEL
        if token_queries:
            futures = []
            for query_type, value in token_queries:
                if query_type == 'get':
                    futures.append(self.token_repo.get(value))
                elif query_type == 'category':
                    futures.append(self.token_repo.list_by_category(value))
                elif query_type == 'class':
                    futures.append(self.token_repo.list_by_class(value))
            
            results = await asyncio.gather(*futures)
            logger.debug(f"Loaded {len(futures)} token queries in parallel")
        else:
            results = []
        
        # PHASE 3: Process results and build assignments
        result_idx = 0
        for slot_entry in resolved_slots:
            slot_type = slot_entry['type']
            value = slot_entry['value']
            is_non_overridable = slot_entry.get('non_overridable', False)
            
            if slot_type == 'slot' and is_non_overridable:
                continue
            
            if slot_type == 'token':
                token = results[result_idx]
                result_idx += 1
                
                if not token:
                    logger.warning(f"Token not found: {value}")
                    continue

                class_name = None
                if value in blueprint.classes:
                    class_name = value
                else:
                    # Prefer class whose default token matches this token
                    for candidate_name, class_schema in blueprint.classes.items():
                        if class_schema.default_token == token.id:
                            class_name = candidate_name
                            break

                if not class_name:
                    candidate_classes = [
                        name for name, class_schema in blueprint.classes.items()
                        if str(token.category) in [
                            str(c) for c in class_schema.allowed_token_categories
                        ]
                    ]
                    if len(candidate_classes) == 1:
                        class_name = candidate_classes[0]
                    else:
                        logger.warning(
                            f"Ambiguous class for token {token.id} (category {token.category}): {candidate_classes}"
                        )

                if class_name and f"{{{{{class_name}}}}}" in blueprint.template:
                    assignments[class_name] = token.id
                    logger.debug(f"Token assignment: {token.id} → {class_name}")
                else:
                    logger.warning(f"Class for token {token.id} not found in template")
                
            elif slot_type == 'category':
                tokens = results[result_idx]
                result_idx += 1
                logger.debug(f"Category '{value}' expanded to {len(tokens)} tokens")
                
                for token in tokens:
                    class_name = self._find_class_for_category(blueprint, token.category)
                    if class_name and class_name not in assignments:
                        # First token wins (no override)
                        assignments[class_name] = token.id
                        logger.debug(f"Assigned {token.id} → {class_name} (from category)")
                        
            elif slot_type == 'class':
                tokens = results[result_idx]
                result_idx += 1
                logger.debug(f"Class '{value}' expanded to {len(tokens)} tokens")
                tokens_by_id = {token.id: token for token in tokens}

                # Prefer blueprint default tokens for each class
                for class_name, class_schema in blueprint.classes.items():
                    default_token = class_schema.default_token
                    if default_token in tokens_by_id and class_name not in assignments:
                        assignments[class_name] = default_token
                        logger.debug(f"Assigned {default_token} → {class_name} (default from class)")

                # Fallback: match by category for any remaining slots
                for token in tokens:
                    class_name = self._find_class_for_category(blueprint, token.category)
                    if class_name and class_name not in assignments:
                        assignments[class_name] = token.id
                        logger.debug(f"Assigned {token.id} → {class_name} (from class)")
            
            elif slot_type == 'slot':
                # Already handled above
                pass
        
        # Apply explicit class exclusions
        for class_name in excluded_classes:
            if class_name in assignments:
                assignments.pop(class_name)
                logger.debug(f"Removed excluded class assignment: {class_name}")

        return assignments
    
    def _find_class_for_category(self, blueprint, category: TokenCategory) -> Optional[str]:
        """Find class name that accepts tokens from given category.
        
        Args:
            blueprint: Blueprint with class schemas
            category: Token category to match
            
        Returns:
            Class name or None if no class accepts this category
        """
        for class_name, class_schema in blueprint.classes.items():
            if str(category) in [
                str(c) for c in class_schema.allowed_token_categories
            ]:
                return class_name
        
        logger.warning(f"No class accepts category: {category}")
        return None

    async def _inject_runtime_context(
        self,
        prompt: str,
        biographical_facts: List[Dict],
        conversation_history: List[dict],
        user_id: str
    ) -> str:
        """Inject RUNTIME data with SecurityPort validation.

        Args:
            prompt: Current prompt template
            biographical_facts: List of biographical facts
            conversation_history: List of conversation messages
            user_id: User ID for logging

        Returns:
            Prompt with validated runtime context injected
        """
        # Validate biographical facts (UNTRUSTED zone)
        if biographical_facts:
            bio_text = self.bio_formatter.format(biographical_facts)
            bio_result = await self.security_port.validate(
                bio_text,
                context=f"biographical_user_{user_id}",
                zone=TrustZone.UNTRUSTED
            )
            validated_bio = bio_result.sanitized_text
            logger.debug(f"Validated biographical facts: {bio_result.risk_level.value}")
        else:
            validated_bio = ""

        # Format and validate conversation history (UNTRUSTED zone)
        if conversation_history:
            formatted_convo = self.formatter.format(conversation_history)
            convo_result = await self.security_port.validate(
                formatted_convo,
                context=f"conversation_user_{user_id}",
                zone=TrustZone.UNTRUSTED
            )
            validated_convo = convo_result.sanitized_text
            logger.debug(f"Validated conversation: {convo_result.risk_level.value}")
        else:
            validated_convo = ""

        # Inject [[CURRENT_DATE_TIME]]
        utc_now = datetime.now(timezone.utc)
        current_datetime = (
            utc_now.strftime("%Y-%m-%d %H:%M %A (UTC)") + "\n        "
            "System time is UTC. The user's local time may differ — "
            "account for timezone when discussing time-sensitive topics."
        )
        prompt = prompt.replace("[[CURRENT_DATE_TIME]]", current_datetime)
        
        # Replace RUNTIME placeholders (using [[...]] notation from universal blueprint)
        prompt = prompt.replace("[[BIOGRAPHICAL_CONTEXT]]", validated_bio)
        prompt = prompt.replace("[[CONVERSATION_HISTORY]]", validated_convo)

        return prompt
    
    def _remove_unreplaced_tokens(self, prompt: str) -> str:
        """Remove unreplaced {{TOKEN}} placeholders from prompt.
        
        If a token was not assigned (not in profile), remove the entire line
        containing {{TOKEN_NAME}}.
        
        Args:
            prompt: Prompt with potential unreplaced {{}} placeholders
            
        Returns:
            Cleaned prompt without {{}} placeholders
        """
        import re
        
        # Remove lines containing {{TOKEN_NAME}}
        lines = prompt.split('\n')
        cleaned_lines = []
        
        for line in lines:
            # Check if line contains {{TOKEN}}
            if re.search(r'\{\{[A-Z_]+\}\}', line):
                logger.debug(f"Removing unreplaced token line: {line.strip()}")
                continue  # Skip this line
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines)

    async def validate_slot_assignment(
        self,
        blueprint_id: str,
        slot_name: str,
        token_id: TokenId,
        owner_type: str
    ) -> bool:
        """Validate if token can be assigned to class.

        Args:
            blueprint_id: Blueprint identifier
            slot_name: Class name
            token_id: Token ID to assign
            owner_type: Owner type (USER, ACCOUNT, AGENT, SYSTEM)

        Returns:
            True if assignment is valid, False otherwise

        Examples:
            >>> valid = await service.validate_slot_assignment(
            ...     "smart_agent_v1",
            ...     "HUMOR_ENGINE",
            ...     TokenId("HUMOR_PRESET_OFF"),
            ...     "USER"
            ... )
        """
        from src.domain.prompt_v3.slot import OwnerType

        blueprint = await self.blueprint_repo.get(blueprint_id)
        token = await self.token_repo.get(token_id)
        owner = OwnerType[owner_type.upper()]

        return blueprint.can_assign(slot_name, token, owner)
    
    # =================================================================
    # CACHE MANAGEMENT (RFC: PROMPT_ASSEMBLY_CACHING_RFC.md)
    # =================================================================
    
    def _build_cache_key(
        self,
        agent_type: str,
        account_id: Optional[str],
        user_id: Optional[str]
    ) -> str:
        """Build cache key from parameters.
        
        Cache key does NOT include runtime data (biographical_facts, conversation_history)
        because they are injected AFTER cache lookup.
        
        Args:
            agent_type: Agent type (e.g., "smart", "quick")
            account_id: Account ID (optional)
            user_id: User ID (optional)
            
        Returns:
            Cache key string
        """
        # Use FULL IDs to ensure uniqueness and stability (fixes truncation bug)
        acc_part = account_id if account_id else "no-acc"
        usr_part = user_id if user_id else "no-usr"
        return f"prompt:{agent_type}:acc:{acc_part}:usr:{usr_part}"
    
    def _get_from_cache(self, key: str) -> Optional[str]:
        """Get value from cache if not expired.
        
        Args:
            key: Cache key
            
        Returns:
            Cached prompt string or None if miss/expired
        """
        if key not in self._assembled_cache:
            return None
        
        content, timestamp = self._assembled_cache[key]
        
        # Check TTL expiry
        if (time.time() - timestamp) >= self._cache_ttl:
            # Expired - remove and return None
            del self._assembled_cache[key]
            logger.debug(f"Cache expired: {key}")
            return None
        
        return content
    
    def _save_to_cache(self, key: str, content: str) -> None:
        """Save value to cache with current timestamp.
        
        Args:
            key: Cache key
            content: Prompt string to cache
        """
        self._assembled_cache[key] = (content, time.time())
        logger.debug(f"Saved to cache: {key} ({len(content)} chars)")
    
    def invalidate_cache(self) -> None:
        """Clear entire cache.
        
        Called by $admin_cache_reset command for debugging.
        Future: Can be extended for granular invalidation (by user_id, account_id).
        """
        cache_size = len(self._assembled_cache)
        self._assembled_cache.clear()
        logger.warning(f"🔥 Cache cleared: {cache_size} entries removed")
    
    async def preload_cache(
        self,
        agent_type: str,
        account_id: str,
        user_id: str
    ) -> None:
        """Preload cache for user after agent initialization.
        
        Called by UserAgentFactory to warm up cache and avoid
        cold start latency on first user request.
        
        Args:
            agent_type: Agent type ("quick" or "smart")
            account_id: Account ID
            user_id: User ID
        """
        cache_key = self._build_cache_key(agent_type, account_id, user_id)
        
        # Skip if already cached
        if cache_key in self._assembled_cache:
            logger.debug(f"📦 Preload skip: {cache_key} already cached")
            return
        
        logger.info(f"📦 Preloading cache: {cache_key}")
        
        # Assemble with empty runtime context
        await self.assemble(
            agent_type=agent_type,
            user_id=user_id,
            account_id=account_id,
            biographical_facts=[],     # Empty for preload
            conversation_history=[]    # Empty for preload
        )
        
        logger.info(f"✅ Cache preloaded: {cache_key}")
