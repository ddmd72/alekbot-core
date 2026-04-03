"""
PromptAssemblyService - Assembles prompts from tokens, blueprints, and profiles.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).

PERFORMANCE OPTIMIZATION (2026-02-04):
- Added assembly cache with 24h TTL
- Parallelized repository calls with asyncio.gather()
- See: docs/10_rfcs/PROMPT_ASSEMBLY_CACHING_RFC.md
"""

import time
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional, Tuple

from src.ports.llm_port import PROMPT_CACHE_BOUNDARY

from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.security_port import SecurityPort, TrustZone
from src.domain.prompt_v3.token import Token, TokenId
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.utils.logger import logger


class PromptAssemblyService:
    """Assembles prompts from token library via blueprint class-collection model (v4).

    The blueprint_id for each agent is read from the agent's profile document in
    Firestore, not hardcoded in the service. This allows changing which blueprint
    an agent uses without a code deployment.
    """

    def __init__(
        self,
        token_repo: TokenRepository,
        blueprint_repo: BlueprintRepository,
        profile_repo: AgentProfileRepository,
        security_port: SecurityPort,
        formatter: ContextFormatter,
        bio_formatter: BiographicalFactsFormatter,
        cache_ttl: int = 86400,  # 24 hours
    ):
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
        conversation_history: Optional[List[dict]] = None,
        query_specific_context: Optional[str] = None,
        kb_preamble: bool = False,
        agent_notes: Optional[List[dict]] = None,
        user_timezone: str = "UTC",
        extra_static_blocks: Optional[List[str]] = None,
    ) -> str:
        """Full prompt assembly with class-collection model + caching.

        PERFORMANCE OPTIMIZATION:
        - Static template (token assembly) cached with 24h TTL
        - Runtime context (biographical facts, conversation) injected AFTER cache lookup
        - Cache key: (agent_type, account_id, user_id) — no runtime data

        Args:
            agent_type: Agent type (e.g., "smart", "quick")
            user_id: User ID for USER-level overrides (optional)
            account_id: Account ID for ACCOUNT-level overrides (optional)
            biographical_facts: Static biographical facts only — no semantic_lens facts.
                The caller (PromptBuilder.build_for_agent) is responsible for separating
                Q-S context before calling assemble().
            conversation_history: List of conversation messages (RUNTIME validation)
            query_specific_context: Pre-formatted query-specific context string from router
                enrichment. Validated here via SecurityPort and appended after the cache
                boundary. None if no Q-S context for this request.

        Returns:
            Assembled prompt string

        Raises:
            KeyError: If blueprint not found
            ValueError: If validation fails
        """
        biographical_facts = biographical_facts or []
        conversation_history = conversation_history or []

        # CACHE CHECK: Build cache key (without runtime data)
        cache_key = self._build_cache_key(agent_type, account_id, user_id)
        cached_template = self._get_from_cache(cache_key)

        if cached_template:
            logger.info(f"📦 Cache HIT: {cache_key}")
            static_prompt = cached_template
        else:
            logger.info(f"📦 Cache MISS: {cache_key} - assembling from repositories...")

            static_prompt = await self._assemble_static_template(
                agent_type=agent_type,
                account_id=account_id,
                user_id=user_id
            )

            self._save_to_cache(cache_key, static_prompt)

        # RUNTIME INJECTION (ALWAYS - even on cache hit)
        # This happens AFTER cache because biographical facts and conversation change frequently
        final_prompt = await self._inject_runtime_context(
            static_prompt,
            biographical_facts,
            conversation_history,
            user_id or "anonymous",
            query_specific_context=query_specific_context,
            kb_preamble=kb_preamble,
            agent_notes=agent_notes,
            user_timezone=user_timezone,
            extra_static_blocks=extra_static_blocks,
        )

        logger.info(f"✅ Assembled prompt: {len(final_prompt)} chars")
        return final_prompt

    async def _assemble_static_template(
        self,
        agent_type: str,
        account_id: Optional[str],
        user_id: Optional[str]
    ) -> str:
        """Assemble static prompt template (cacheable part) using v4 class-collection model.

        Steps:
        1. Load agent profile — contains blueprint_id + agent token map
        2. Load blueprint + overrides in parallel (asyncio.gather)
        3. Apply account overrides (class+category match, respect non_overridable)
        4. Apply user overrides on top (same rules)
        5. Fetch all active token documents in parallel
        6. Group tokens by class, sort by ProfileToken.order within each class
        7. Render each section: "    {class_name} {\\n\\n    {content}\\n\\n    }"
        8. Wrap in outer class: "class {outer_class} {\\n\\n{sections}\\n\\n}"

        Args:
            agent_type: Agent type (= agent_id in profile lookup, e.g. "quick", "smart")
            account_id: Account ID (optional)
            user_id: User ID (optional)

        Returns:
            Static prompt template (without runtime context)
        """
        # STEP 1: Load agent profile — blueprint_id lives in the profile document
        agent_profile = await self.profile_repo.get_agent_profile(agent_type)
        blueprint_id = agent_profile.blueprint_id
        agent_tokens: Dict[str, ProfileToken] = agent_profile.tokens

        # STEP 2: Load blueprint + overrides in parallel
        tasks: List[Any] = [self.blueprint_repo.get(blueprint_id)]
        account_override_idx = -1
        user_override_idx = -1
        if account_id:
            account_override_idx = len(tasks)
            tasks.append(
                self.profile_repo.get_override_tokens(OwnerType.ACCOUNT, account_id)
            )
        if user_id:
            user_override_idx = len(tasks)
            tasks.append(
                self.profile_repo.get_override_tokens(OwnerType.USER, user_id)
            )

        results = await asyncio.gather(*tasks)

        blueprint = results[0]
        account_overrides: Dict[str, ProfileToken] = (
            results[account_override_idx] if account_override_idx >= 0 else {}
        )
        user_overrides: Dict[str, ProfileToken] = (
            results[user_override_idx] if user_override_idx >= 0 else {}
        )

        logger.debug(
            f"Loaded blueprint={blueprint_id}, agent_tokens={len(agent_tokens)}, "
            f"account_overrides={len(account_overrides)}, user_overrides={len(user_overrides)}"
        )

        # STEP 2: Collect all token IDs that must be fetched from the token repository
        all_token_ids = (
            set(agent_tokens.keys()) | set(account_overrides.keys()) | set(user_overrides.keys())
        )

        if not all_token_ids:
            logger.warning(f"No tokens found for agent_type={agent_type}")
            return f"class {blueprint.outer_class} {{\n\n}}"

        # STEP 3: Fetch all token documents in parallel (exceptions captured, not raised)
        token_id_list = list(all_token_ids)
        token_docs_raw = await asyncio.gather(
            *[self.token_repo.get(TokenId(tid)) for tid in token_id_list],
            return_exceptions=True
        )

        token_docs: Dict[str, Token] = {}
        for tid, doc in zip(token_id_list, token_docs_raw):
            if isinstance(doc, Exception):
                logger.warning(f"Failed to fetch token {tid}: {doc}")
            else:
                token_docs[tid] = doc

        # STEP 4: Build initial active set from agent tokens
        # active: Dict[token_id, Tuple[ProfileToken, Token]]
        active: Dict[str, Tuple[ProfileToken, Token]] = {}
        for tid, pt in agent_tokens.items():
            if tid in token_docs:
                active[tid] = (pt, token_docs[tid])
            else:
                logger.warning(f"Agent token {tid} not found in token repository — skipped")

        # STEP 5: Apply account-level overrides
        if account_overrides:
            active = self._apply_overrides(active, account_overrides, token_docs, "ACCOUNT")

        # STEP 6: Apply user-level overrides
        if user_overrides:
            active = self._apply_overrides(active, user_overrides, token_docs, "USER")

        # STEP 7: Group active tokens by class, collect (order, Token) pairs
        by_class: Dict[str, List[Tuple[int, Token]]] = defaultdict(list)
        for tid, (pt, tok) in active.items():
            by_class[str(tok.class_)].append((pt.order, tok))

        # STEP 8: Render sections in blueprint class_order
        sections = []
        for class_name in blueprint.class_order:
            entries = by_class.get(class_name, [])
            if not entries:
                continue

            entries.sort(key=lambda x: x[0])  # sort by ProfileToken.order

            content_blocks = []
            for _, tok in entries:
                # Indent each non-empty line of bare token content by 8 spaces
                indented_lines = []
                for line in tok.content.split("\n"):
                    indented_lines.append(f"        {line}" if line.strip() else "")
                content_blocks.append("\n".join(indented_lines))

            combined = "\n\n".join(content_blocks)
            sections.append(f"    {class_name} {{\n\n{combined}\n\n    }}")

        # STEP 9: Wrap in outer class
        sections_str = "\n\n".join(sections)
        prompt = f"class {blueprint.outer_class} {{\n\n{sections_str}\n\n}}"

        prompt = self._normalize_whitespace(prompt)

        logger.debug(f"Static template assembled: {len(prompt)} chars, {len(sections)} sections")
        return prompt

    def _apply_overrides(
        self,
        active: Dict[str, Tuple[ProfileToken, Token]],
        override_tokens: Dict[str, ProfileToken],
        token_docs: Dict[str, Token],
        level: str,
    ) -> Dict[str, Tuple[ProfileToken, Token]]:
        """Apply override tokens to active set by class+category matching.

        For each override token:
          - Find an active token with the same class AND same category.
          - If found and active token is NOT non_overridable → replace.
          - If no match found → ignore (cannot add new classes to agent profile).

        Args:
            active: Current active tokens {token_id: (ProfileToken, Token)}
            override_tokens: Override profile map {token_id: ProfileToken}
            token_docs: All fetched token documents {token_id: Token}
            level: Override level label for logging ("ACCOUNT" or "USER")

        Returns:
            Updated active dict with overrides applied (original is not mutated).
        """
        result = dict(active)

        for override_tid, override_pt in override_tokens.items():
            if override_tid not in token_docs:
                logger.warning(f"[{level}] Override token {override_tid} not found in repository")
                continue

            override_tok = token_docs[override_tid]
            override_class = str(override_tok.class_)
            override_category = str(override_tok.category)

            # Find active token with same class + category
            matched_active_tid: Optional[str] = None
            is_blocked = False

            for active_tid, (active_pt, active_tok) in result.items():
                if (
                    str(active_tok.class_) == override_class
                    and str(active_tok.category) == override_category
                ):
                    if active_pt.non_overridable:
                        logger.debug(
                            f"[{level}] Skip override {override_tid}: "
                            f"{active_tid} is non_overridable"
                        )
                        is_blocked = True
                    else:
                        matched_active_tid = active_tid
                    break

            if is_blocked:
                continue

            if matched_active_tid is not None:
                del result[matched_active_tid]
                result[override_tid] = (override_pt, override_tok)
                logger.debug(
                    f"[{level}] Override: {matched_active_tid} → {override_tid} "
                    f"(class={override_class}, category={override_category})"
                )
            else:
                logger.debug(
                    f"[{level}] Override token {override_tid} ignored: "
                    f"no active token with class={override_class}+category={override_category}"
                )

        return result

    async def _inject_runtime_context(
        self,
        prompt: str,
        biographical_facts: List[Dict],
        conversation_history: List[dict],
        user_id: str,
        query_specific_context: Optional[str] = None,
        kb_preamble: bool = False,
        agent_notes: Optional[List[dict]] = None,
        user_timezone: str = "UTC",
        extra_static_blocks: Optional[List[str]] = None,
    ) -> str:
        """Inject RUNTIME data with SecurityPort validation.

        Args:
            prompt: Current prompt template (assembled blueprint)
            biographical_facts: Static biographical facts (no semantic_lens facts — caller splits).
            conversation_history: List of conversation messages
            user_id: User ID for logging
            query_specific_context: Pre-formatted Q-S context string from router enrichment.
                Validated here via SecurityPort and appended after the cache boundary.
            kb_preamble: When True, knowledge_base {} is placed BEFORE the blueprint template
                (preamble). When False (default), it is appended after (postamble).

        Returns:
            Prompt with validated runtime context injected
        """
        # Validate static biographical facts (UNTRUSTED zone)
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

        # Validate query-specific context (UNTRUSTED zone)
        if query_specific_context:
            qs_result = await self.security_port.validate(
                query_specific_context,
                context=f"semantic_user_{user_id}",
                zone=TrustZone.UNTRUSTED
            )
            query_specific_str = qs_result.sanitized_text
            logger.debug(f"Validated query-specific context: {qs_result.risk_level.value}")
        else:
            query_specific_str = ""

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

        # Build current datetime string in user's local timezone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            user_tz = ZoneInfo(user_timezone or "UTC")
        except (ZoneInfoNotFoundError, KeyError):
            user_tz = ZoneInfo("UTC")
        local_now = datetime.now(user_tz)
        tz_abbr = local_now.strftime("%Z") or user_timezone or "UTC"
        current_datetime = local_now.strftime(f"%Y-%m-%d %H:%M %A ({tz_abbr})")

        # Static runtime blocks — appended after the blueprint template (before the boundary).
        # Blueprint no longer contains [[BIOGRAPHICAL_CONTEXT]] / [[CONVERSATION_HISTORY]]
        # placeholders; these blocks are built conditionally here so empty wrappers never appear.
        #
        # For smart/quick: validated_bio may be non-empty; validated_convo is always "".
        # For consolidation: validated_convo contains the history batch (fixed per run, cached).
        kb_parts = []
        if validated_bio:
            bio_header = "    // Personal facts about the user. Dates = when recorded. Treat older entries as potentially stale; conversation history takes precedence if contradictory."
            kb_parts.append(f"    biographical_context: '''\n{bio_header}\n{validated_bio}\n    '''")
        if validated_convo:
            kb_parts.append(f"    conversation_history: '''\n{validated_convo}\n    '''")

        if kb_parts:
            kb_block = "knowledge_base {\n" + "\n\n".join(kb_parts) + "\n}"
            if kb_preamble:
                # Preamble: context first, instructions last → better recency for cognitive_process
                extra = ("\n\n" + "\n\n".join(extra_static_blocks)) if extra_static_blocks else ""
                prompt = kb_block + extra + "\n\n" + prompt
            else:
                # Postamble: default — knowledge_base appended after blueprint
                prompt = prompt + "\n\n" + kb_block
        elif extra_static_blocks and kb_preamble:
            prompt = "\n\n".join(extra_static_blocks) + "\n\n" + prompt

        # Append cache boundary + dynamic content
        dynamic_parts = []

        # pending_notes — orchestrator-produced, TRUSTED zone, no SecurityPort validation
        if agent_notes:
            note_lines = []
            for note in agent_notes:
                note_id = note.get("note_id", "")
                text = note.get("text", "")
                due = note.get("due")
                expires_after = note.get("expires_after")
                if due:
                    try:
                        due_dt = datetime.fromisoformat(str(due))
                        timing_str = f" (fires: {due_dt.strftime('%b %d %H:%M UTC')})"
                    except (ValueError, TypeError):
                        timing_str = f" (fires: {due})"
                elif expires_after:
                    try:
                        exp_dt = datetime.fromisoformat(str(expires_after))
                        timing_str = f" (expires: {exp_dt.strftime('%b %d %H:%M UTC')})"
                    except (ValueError, TypeError):
                        timing_str = f" (expires: {expires_after})"
                else:
                    timing_str = ""
                line = f"    - {text}{timing_str} [id: {note_id}]"
                note_lines.append(line)
            header = "    // Reminders you set for yourself. Not visible to the user. Snapshot from turn start.\n    // Full execution context is stored internally. To update or delete — delegate with the id shown in brackets.\n    // IDs are Unix timestamps (ms) — use to gauge reminder age relative to current_date_time."
            dynamic_parts.append("active_reminders {\n" + header + "\n" + "\n".join(note_lines) + "\n}")

        dynamic_parts.append(f"current_date_time {{\n    {current_datetime}\n}}")
        if query_specific_str:
            dynamic_parts.append(f"query_specific_context: '''\n{query_specific_str}\n'''")
        prompt = prompt + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + "\n\n".join(dynamic_parts)

        return prompt

    def _normalize_whitespace(self, prompt: str) -> str:
        """Collapse empty structural blocks and excessive blank lines.

        After section rendering, some edge cases may leave empty blocks or
        excessive blank lines. This method:
          1. Removes blocks whose body is entirely whitespace (single-depth match).
          2. Collapses 3+ consecutive blank lines to 2 newlines.

        Args:
            prompt: Prompt string after token assembly.

        Returns:
            Cleaned prompt with empty blocks and excessive blank lines removed.
        """
        import re

        # Remove empty single-depth blocks: "word_chars { <whitespace only> }"
        prompt = re.sub(
            r'\n[ \t]*\w[\w_]*[ \t]*\{[ \t\n]*\}',
            '',
            prompt,
        )

        # Collapse 3+ consecutive newlines to 2
        prompt = re.sub(r'\n{3,}', '\n\n', prompt)

        return prompt

    # =================================================================
    # CACHE MANAGEMENT (RFC: PROMPT_ASSEMBLY_CACHING_RFC.md)
    # =================================================================

    def _build_cache_key(
        self,
        agent_type: str,
        account_id: Optional[str],
        user_id: Optional[str],
    ) -> str:
        """Build cache key from parameters.

        Cache key does NOT include runtime data (biographical_facts, conversation_history)
        because they are injected AFTER cache lookup. Language state is captured via
        user_id — user-level profile overrides (LANG_FIXED_*) are fetched during
        static assembly and vary per user.
        """
        acc_part = account_id if account_id else "no-acc"
        usr_part = user_id if user_id else "no-usr"
        return f"prompt:{agent_type}:acc:{acc_part}:usr:{usr_part}"

    def _get_from_cache(self, key: str) -> Optional[str]:
        """Get value from cache if not expired."""
        if key not in self._assembled_cache:
            return None

        content, timestamp = self._assembled_cache[key]

        if (time.time() - timestamp) >= self._cache_ttl:
            del self._assembled_cache[key]
            logger.debug(f"Cache expired: {key}")
            return None

        return content

    def _save_to_cache(self, key: str, content: str) -> None:
        """Save value to cache with current timestamp."""
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

        if cache_key in self._assembled_cache:
            logger.debug(f"📦 Preload skip: {cache_key} already cached")
            return

        logger.info(f"📦 Preloading cache: {cache_key}")

        await self.assemble(
            agent_type=agent_type,
            user_id=user_id,
            account_id=account_id,
            biographical_facts=[],
            conversation_history=[]
        )

        logger.info(f"✅ Cache preloaded: {cache_key}")
