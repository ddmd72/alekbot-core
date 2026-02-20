"""
Prompt Component Service - coordination layer for component-based prompts.

Orchestrates loading, merging, and assembly of prompt components.

Session 23: Prompt Component Architecture (Service Layer - 2-level merge)
Session 25: Integration with 3-level priority resolution

RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
Purpose: Coordinate Domain ↔ Adapters for component-based prompt assembly

SESSION_25 Changes:
- Added agent_type parameter (required for AGENT-level resolution)
- Use repository.resolve_component() for 3-level priority
- Updated cache key to include agent_type
- Legacy 2-level merge preserved in comments
"""

from typing import List, Optional, Dict, Union
from src.domain.prompt import PromptComponent, PromptTemplate, ComponentScope
from src.ports.prompt_component_repository import PromptComponentRepository
from src.ports.prompt_assembler import PromptAssembler
from src.utils.logger import logger
import time


class PromptComponentService:
    """
    Service layer for prompt component management.
    
    SESSION_25: Uses 3-level resolution (USER > AGENT > SYSTEM)
    
    Responsibilities:
    - Resolve components using repository.resolve_component()
    - Filter by template scopes
    - Assemble prompts
    - Cache assembled results
    - Coordinate with repository and assembler adapters
    
    Usage:
        service = PromptComponentService(repo, assembler)
        prompt = await service.get_assembled_prompt(
            template=TEMPLATE_LIGHT,
            agent_type="quick",
            user_id="user123"
        )
    """
    
    def __init__(
        self,
        repository: PromptComponentRepository,
        assembler: Union[PromptAssembler, Dict[str, PromptAssembler]],
        cache_ttl: int = 3600
    ):
        """
        Initialize service with adapters.

        Args:
            repository: Storage adapter for components
            assembler: Assembly adapter(s). Can be:
                - Single assembler (backward compatibility)
                - Dict of assemblers by format: {"groovy": GroovyAssembler, "xml": XmlAssembler}
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        """
        self.repository = repository

        # Support both single assembler (legacy) and dict of assemblers (new)
        if isinstance(assembler, dict):
            self.assemblers = assembler
            self.assembler = assembler.get("groovy")  # Default for backward compat
        else:
            self.assembler = assembler
            self.assemblers = {"groovy": assembler}

        self.cache_ttl = cache_ttl

        # Cache format: {cache_key: (assembled_prompt, timestamp)}
        self._cache: Dict[str, tuple] = {}
    
    async def get_assembled_prompt(
        self,
        template: PromptTemplate,
        agent_type: str,
        user_id: str,      # SESSION_26: REQUIRED (was Optional) - use ANONYMOUS_USER_ID for unreg
        account_id: str,   # SESSION_26: REQUIRED (was Optional) - use ANONYMOUS_ACCOUNT_ID for unreg
        scope_filter: Optional[ComponentScope] = None
    ) -> str:
        """
        Get fully assembled prompt with 4-level priority resolution.

        SESSION_25: agent_type is REQUIRED for AGENT-level resolution.
        SESSION_26: user_id and account_id are REQUIRED (no Optional/None).

        For unregistered users, use:
        - user_id=ANONYMOUS_USER_ID
        - account_id=ANONYMOUS_ACCOUNT_ID

        For system operations, use:
        - user_id=SYSTEM_USER_ID
        - account_id=SYSTEM_ACCOUNT_ID

        NOTE: user_id and account_id are execution context parameters.
        See: docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md

        Args:
            template: Template defining structure (TEMPLATE_LIGHT, TEMPLATE_FULL)
            agent_type: Agent type ("quick", "smart", "router", "consolidation")
            user_id: REQUIRED user ID (use ANONYMOUS_USER_ID if not registered)
            account_id: REQUIRED account ID (use ANONYMOUS_ACCOUNT_ID if not registered)
            scope_filter: Optional filter to load only specific scope

        Returns:
            Assembled prompt string (format determined by template.output_format)
        """
        # Check cache (now includes account_id)
        cache_key = self._build_cache_key(template.name, agent_type, user_id, account_id, scope_filter)
        cached_result = self._get_from_cache(cache_key)

        if cached_result:
            logger.debug(f"📦 [PromptComponentService] Cache hit for {cache_key}")
            return cached_result

        logger.debug(f"📦 [PromptComponentService] Cache miss for {cache_key}, resolving components...")

        # SESSION_26: Resolve components using 4-level priority
        components = await self._resolve_components(template, agent_type, account_id, user_id, scope_filter)

        # SESSION_26: Select assembler based on template output format
        output_format = getattr(template, 'output_format', 'groovy')
        assembler = self.assemblers.get(output_format, self.assembler)

        if not assembler:
            raise ValueError(f"No assembler found for format '{output_format}'")

        # Assemble final prompt (signature: template, components)
        assembled = assembler.assemble(template, components)

        # Cache result
        self._save_to_cache(cache_key, assembled)

        logger.info(f"✅ [PromptComponentService] Assembled {len(components)} components for {agent_type}/{template.name} (format={output_format})")
        
        return assembled
    
    async def _resolve_components(
        self,
        template: PromptTemplate,
        agent_type: str,
        account_id: str,   # SESSION_26: REQUIRED (was Optional)
        user_id: str,      # SESSION_26: REQUIRED (was Optional)
        scope_filter: Optional[ComponentScope]
    ) -> List[PromptComponent]:
        """
        Resolve all components for template using 4-level priority.

        SESSION_25: Uses repository.resolve_component() for each component_id.
        SESSION_26: Extended to 4-level with ACCOUNT. Made user_id/account_id REQUIRED.

        Args:
            template: Template with required scopes
            agent_type: Agent type for AGENT-level resolution
            account_id: Optional account for ACCOUNT-level resolution
            user_id: Optional user for USER-level resolution
            scope_filter: Optional scope filter

        Returns:
            List of resolved components (excluding EXCLUDED ones)
        """
        # Get all SYSTEM components to know which component_ids exist
        system_components = await self.repository.get_default_components(scope_filter)

        # Resolve each component using 4-level priority
        resolved = []
        for system_comp in system_components:
            # Only resolve if in template scopes
            if system_comp.scope not in template.scopes:
                logger.debug(f"⏭️ Skipping '{system_comp.id}' (scope {system_comp.scope.value} not in template)")
                continue

            # Resolve with 4-level priority: USER > ACCOUNT > AGENT > SYSTEM
            component = await self.repository.resolve_component(
                component_id=system_comp.id,
                agent_type=agent_type,
                account_id=account_id,
                user_id=user_id
            )
            
            if component:
                resolved.append(component)
            else:
                logger.debug(f"🚫 Component '{system_comp.id}' EXCLUDED or not found")
        
        # Sort by order
        resolved.sort(key=lambda c: c.order)
        
        logger.debug(f"📦 Resolved {len(resolved)}/{len(system_components)} components for {agent_type}")
        
        return resolved
    
    async def get_components_for_user(
        self,
        user_id: str,
        scope: Optional[ComponentScope] = None
    ) -> List[PromptComponent]:
        """
        Get USER-level overrides for debugging/inspection.
        
        Args:
            user_id: User identifier
            scope: Optional scope filter
            
        Returns:
            List of USER components only (not resolved)
        """
        return await self.repository.get_user_overrides(user_id, scope)
    
    async def save_user_override(
        self,
        user_id: str,
        component: PromptComponent
    ) -> None:
        """
        Save user-specific component override.
        
        Args:
            user_id: User identifier
            component: Component to save
        """
        await self.repository.save_user_override(user_id, component)
        
        # Invalidate cache for this user
        self.invalidate_cache(user_id=user_id)
        
        logger.info(f"💾 [PromptComponentService] Saved USER override: {component.id} for {user_id[:8]}")
    
    async def delete_user_override(
        self,
        user_id: str,
        component_id: str
    ) -> bool:
        """
        Delete user-specific component override.
        
        Args:
            user_id: User identifier
            component_id: Component ID to delete
            
        Returns:
            True if deleted, False if not found
        """
        deleted = await self.repository.delete_user_override(user_id, component_id)
        
        if deleted:
            # Invalidate cache for this user
            self.invalidate_cache(user_id=user_id)
            logger.info(f"🗑️ [PromptComponentService] Deleted USER override: {component_id} for {user_id[:8]}")
        
        return deleted
    
    def _build_cache_key(
        self,
        template_name: str,
        agent_type: str,  # SESSION_25: NEW parameter
        user_id: Optional[str],
        account_id: Optional[str],  # SESSION_26: NEW parameter
        scope_filter: Optional[ComponentScope]
    ) -> str:
        """
        Build cache key from parameters.

        SESSION_25: Includes agent_type in key.
        SESSION_26: Includes account_id in key.
        """
        parts = [f"prompt:{template_name}"]
        parts.append(f"agent:{agent_type}")  # SESSION_25

        # SESSION_26: Add account_id to cache key
        if account_id:
            parts.append(f"account:{account_id[:8]}")
        else:
            parts.append("no-account")

        if user_id:
            parts.append(f"user:{user_id[:8]}")
        else:
            parts.append("no-user")

        if scope_filter:
            parts.append(f"scope:{scope_filter.value}")

        return ":".join(parts)
    
    def _get_from_cache(self, cache_key: str) -> Optional[str]:
        """Get value from cache if not expired."""
        if cache_key not in self._cache:
            return None
        
        content, timestamp = self._cache[cache_key]
        current_time = time.time()
        
        if (current_time - timestamp) >= self.cache_ttl:
            # Expired
            del self._cache[cache_key]
            return None
        
        return content
    
    def _save_to_cache(self, cache_key: str, content: str) -> None:
        """Save value to cache with current timestamp."""
        self._cache[cache_key] = (content, time.time())
    
    def invalidate_cache(self, user_id: Optional[str] = None, agent_type: Optional[str] = None) -> None:
        """
        Invalidate cache entries.
        
        SESSION_25: Can invalidate by user_id or agent_type.
        
        Args:
            user_id: If provided, invalidate entries for this user
            agent_type: If provided, invalidate entries for this agent
                       If both None, invalidate all cache
        """
        if user_id is None and agent_type is None:
            self._cache.clear()
            logger.info("📦 [PromptComponentService] Invalidated entire cache")
            return
        
        keys_to_remove = []
        
        if user_id:
            user_key_fragment = f"user:{user_id[:8]}"
            keys_to_remove.extend([k for k in self._cache.keys() if user_key_fragment in k])
        
        if agent_type:
            agent_key_fragment = f"agent:{agent_type}"
            keys_to_remove.extend([k for k in self._cache.keys() if agent_key_fragment in k])
        
        # Remove duplicates
        keys_to_remove = list(set(keys_to_remove))
        
        for key in keys_to_remove:
            del self._cache[key]
        
        logger.info(f"📦 [PromptComponentService] Invalidated {len(keys_to_remove)} cache entries")
    
    def get_cache_stats(self) -> Dict:
        """Get cache statistics for monitoring."""
        total_entries = len(self._cache)
        current_time = time.time()
        
        expired_count = 0
        for content, timestamp in self._cache.values():
            if (current_time - timestamp) >= self.cache_ttl:
                expired_count += 1
        
        return {
            'total_entries': total_entries,
            'expired_entries': expired_count,
            'cache_ttl_seconds': self.cache_ttl,
            'cache_hit_ratio_estimate': 1.0 - (expired_count / max(total_entries, 1))
        }


# =============================================================================
# LEGACY CODE (Session 23 - 2-level merge)
# =============================================================================
# Preserved for reference. SESSION_25 uses 3-level resolution instead.
#
# async def _load_merged_components_LEGACY(user_id, scope_filter):
#     """OLD 2-level merge: defaults + user overrides."""
#     defaults = await repository.get_default_components(scope_filter)
#     if not user_id:
#         return defaults
#     
#     overrides = await repository.get_user_overrides(user_id, scope_filter)
#     # Simple dict merge by component.id
#     override_map = {comp.id: comp for comp in overrides}
#     merged = [override_map.get(d.id, d) for d in defaults]
#     return merged
#
# =============================================================================
